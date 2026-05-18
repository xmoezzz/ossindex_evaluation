#!/usr/bin/env python3
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

OUTPUT_LOG_LIMIT = 20000

@dataclass(frozen=True)
class JobSpec:
    job_id: str
    job_name: str
    safe_name: str
    jobs_dir: Path
    docker_image: str
    timeout_seconds: int
    archive_path: Path

    @property
    def job_dir(self) -> Path:
        return self.jobs_dir / self.job_id


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value.strip())


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        cleaned = "job"
    if len(cleaned) <= 96:
        return cleaned
    digest = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{cleaned[:80].rstrip('._-')}-{digest}"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def update_status(job_dir: Path, **updates: Any) -> None:
    status_path = job_dir / "status.json"
    payload = read_json(status_path) if status_path.exists() else {}
    payload.update(updates)
    payload["updated_at"] = utc_now()
    write_json(status_path, payload)


def tail_text(path: Path, limit: int = OUTPUT_LOG_LIMIT) -> str:
    if not path.is_file():
        return ""
    data = path.read_bytes()
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace")


def artifact_list(job_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(job_dir.rglob("*"), key=lambda p: str(p)):
        if path.is_file():
            out.append({"path": path.relative_to(job_dir).as_posix(), "size_bytes": path.stat().st_size})
    return out


def safe_extract_tar(archive_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with tarfile.open(archive_path, "r:*") as tf:
        members = tf.getmembers()
        for member in members:
            if member.issym() or member.islnk():
                raise RuntimeError(f"archive contains unsupported link entry: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"archive contains unsupported special entry: {member.name}")
            target = (dest_dir / member.name).resolve()
            if target != root and not str(target).startswith(str(root) + os.sep):
                raise RuntimeError(f"unsafe archive entry: {member.name}")
        tf.extractall(dest_dir, members=members)


def normalize_source_root(source_dir: Path) -> Path:
    entries = [p for p in source_dir.iterdir() if not p.name.startswith(".")]
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return source_dir


def ensure_docker() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("docker not found in PATH")


def worker_name(jobs_dir: Path, docker_image: str, slot: int) -> str:
    raw = f"{jobs_dir.resolve()}::{docker_image}::{slot}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"movery-worker-{slot}-{digest}"

_WORKER_CONTAINERS: set[str] = set()
_WORKER_CONTAINER_LOCK = threading.Lock()


def cleanup_worker_containers() -> None:
    for name in list(_WORKER_CONTAINERS):
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

atexit.register(cleanup_worker_containers)


def ensure_worker_container(jobs_dir: Path, docker_image: str, slot: int) -> str:
    ensure_docker()
    jobs_dir.mkdir(parents=True, exist_ok=True)
    name = worker_name(jobs_dir, docker_image, slot)
    with _WORKER_CONTAINER_LOCK:
        inspect = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if inspect.returncode == 0:
            _WORKER_CONTAINERS.add(name)
            return name
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        cmd = [
            "docker", "run", "-d", "--name", name,
            "-e", "PYTHONUNBUFFERED=1",
            "-v", f"{jobs_dir.resolve()}:/jobs",
            docker_image,
            "tail", "-f", "/dev/null",
        ]
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"failed to start MOVERY worker container {name}: {completed.stderr.strip()}")
        _WORKER_CONTAINERS.add(name)
        return name


def run_docker_job(spec: JobSpec, slot: int) -> None:
    job_dir = spec.job_dir
    stdout_path = job_dir / "stdout.log"
    stderr_path = job_dir / "stderr.log"
    source_dir = job_dir / "source"

    if source_dir.exists():
        shutil.rmtree(source_dir)
    safe_extract_tar(spec.archive_path, source_dir)
    scan_root = normalize_source_root(source_dir)
    if scan_root != source_dir:
        normalized = job_dir / "normalized_source"
        if normalized.exists():
            shutil.rmtree(normalized)
        shutil.copytree(scan_root, normalized, symlinks=False)
        scan_root = normalized

    worker = ensure_worker_container(spec.jobs_dir, spec.docker_image, slot)
    target_name = f"job-{spec.safe_name}-{spec.job_id[:8]}"
    container_script = r'''
set -euo pipefail
JOB_ID="${MOVERY_JOB_ID:?missing MOVERY_JOB_ID}"
TARGET="${MOVERY_TARGET_NAME:?missing MOVERY_TARGET_NAME}"

MOVERY_ROOT="/home/MOVERY"
PREPROCESS_SCRIPT="$MOVERY_ROOT/Preprocessing.py"
DETECTOR_SCRIPT="$MOVERY_ROOT/Detector.py"

if [ ! -d "$MOVERY_ROOT" ]; then
    echo "[MOVERY_STAGE] missing MOVERY root: $MOVERY_ROOT"
    exit 2
fi

if [ ! -f "$PREPROCESS_SCRIPT" ]; then
    echo "[MOVERY_STAGE] missing preprocessing script: $PREPROCESS_SCRIPT"
    echo "[MOVERY_STAGE] /home/MOVERY contents:"
    ls -lah "$MOVERY_ROOT" || true
    exit 2
fi

if [ ! -f "$DETECTOR_SCRIPT" ]; then
    echo "[MOVERY_STAGE] missing detector script: $DETECTOR_SCRIPT"
    echo "[MOVERY_STAGE] /home/MOVERY contents:"
    ls -lah "$MOVERY_ROOT" || true
    exit 2
fi

cd "$MOVERY_ROOT"
rm -rf "$TARGET"
cp -a "/jobs/${JOB_ID}/normalized_source" "$TARGET" 2>/dev/null || cp -a "/jobs/${JOB_ID}/source" "$TARGET"

echo "[MOVERY_STAGE] job=${JOB_ID} target=${TARGET} root=${MOVERY_ROOT} start $(date -Is)"
echo "[MOVERY_STAGE] scripts:"
ls -l "$PREPROCESS_SCRIPT" "$DETECTOR_SCRIPT" || true
echo "[MOVERY_STAGE] target sample:"
find "$TARGET" -maxdepth 3 -type f | head -100 || true

run_stage() {
    stage="$1"
    shift
    echo "[MOVERY_STAGE] start ${stage} $(date -Is)"
    set +e
    "$@" 2>&1
    rc=$?
    set -e
    echo "[MOVERY_STAGE] done ${stage} rc=${rc} $(date -Is)"
    return "$rc"
}

run_stage Preprocessing python3 "$PREPROCESS_SCRIPT" "$TARGET"
run_stage Detector python3 "$DETECTOR_SCRIPT" "$TARGET" 0

echo "[MOVERY_STAGE] output files under target:"
find "$TARGET" -maxdepth 4 -type f | head -200 || true
echo "[MOVERY_STAGE] job=${JOB_ID} target=${TARGET} finished $(date -Is)"
rm -rf "$TARGET"
'''.strip()
    cmd = [
        "docker", "exec",
        "-e", f"MOVERY_JOB_ID={spec.job_id}",
        "-e", f"MOVERY_TARGET_NAME={target_name}",
        worker, "bash", "-lc", container_script,
    ]
    write_json(job_dir / "command.json", {"argv": cmd, "worker_container": worker, "slot": slot})
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        stdout.write(("$ " + " ".join(cmd) + "\n").encode("utf-8", errors="replace"))
        stdout.flush()
        try:
            completed = subprocess.run(cmd, stdout=stdout, stderr=stderr, timeout=spec.timeout_seconds if spec.timeout_seconds > 0 else None, check=False)
        except subprocess.TimeoutExpired:
            stderr.write(f"command timed out after {spec.timeout_seconds} seconds\n".encode("utf-8"))
            stderr.flush()
            raise RuntimeError(f"MOVERY job timed out after {spec.timeout_seconds} seconds")
    if completed.returncode != 0:
        raise RuntimeError(f"MOVERY command failed with exit code {completed.returncode}")


def build_result(spec: JobSpec) -> dict[str, Any]:
    job_dir = spec.job_dir
    return {
        "job_id": spec.job_id,
        "job_name": spec.job_name,
        "safe_name": spec.safe_name,
        "status": "succeeded",
        "stdout_tail": tail_text(job_dir / "stdout.log"),
        "stderr_tail": tail_text(job_dir / "stderr.log"),
        "artifacts": artifact_list(job_dir),
    }

JOBS_DIR = Path(env_str("MOVERY_JOBS_DIR", "jobs")).expanduser().resolve()
DOCKER_IMAGE = env_str("MOVERY_DOCKER_IMAGE", "seunghoonwoo/movery-public:latest")
SERVER_WORKERS = env_int("MOVERY_SERVER_WORKERS", 1)
QUEUE_MAX_SIZE = env_int("MOVERY_QUEUE_MAX_SIZE", max(8, SERVER_WORKERS * 4))

app = FastAPI(title="MOVERY service", version="0.1")
job_queue: queue.Queue[JobSpec] = queue.Queue(maxsize=QUEUE_MAX_SIZE)
worker_states: dict[int, dict[str, Any]] = {}
worker_states_lock = threading.Lock()


def set_worker_state(slot: int, **updates: Any) -> None:
    with worker_states_lock:
        state = worker_states.setdefault(slot, {"slot": slot, "state": "idle", "job_id": None, "updated_at": utc_now()})
        state.update(updates)
        state["updated_at"] = utc_now()


def capacity_payload() -> dict[str, Any]:
    with worker_states_lock:
        busy = sum(1 for s in worker_states.values() if s.get("state") == "running")
    queued = job_queue.qsize()
    idle = max(0, SERVER_WORKERS - busy)
    return {
        "server_workers": SERVER_WORKERS,
        "busy_workers": busy,
        "idle_workers": idle,
        "queued_jobs": queued,
        "queue_max_size": QUEUE_MAX_SIZE,
        "can_accept": not job_queue.full(),
    }


def worker_loop(slot: int) -> None:
    set_worker_state(slot, state="idle", job_id=None)
    while True:
        spec = job_queue.get()
        set_worker_state(slot, state="running", job_id=spec.job_id)
        try:
            update_status(spec.job_dir, job_id=spec.job_id, status="running", error=None)
            run_docker_job(spec, slot)
            result = build_result(spec)
            write_json(spec.job_dir / "result.json", result)
            update_status(spec.job_dir, status="succeeded", error=None, result_available=True)
        except Exception as exc:
            update_status(spec.job_dir, status="failed", error=str(exc), result_available=False)
        finally:
            set_worker_state(slot, state="idle", job_id=None)
            job_queue.task_done()


@app.on_event("startup")
def on_startup() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for slot in range(SERVER_WORKERS):
        thread = threading.Thread(target=worker_loop, args=(slot,), name=f"movery-worker-loop-{slot}", daemon=True)
        thread.start()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "jobs_dir": str(JOBS_DIR), "docker_image": DOCKER_IMAGE, **capacity_payload()}


@app.get("/capacity")
def capacity() -> dict[str, Any]:
    return capacity_payload()


@app.post("/scan_archive")
def scan_archive(file: UploadFile = File(...), job_name: Optional[str] = Form(default=None), timeout_seconds: int = Form(default=0)) -> dict[str, Any]:
    if job_queue.full():
        raise HTTPException(status_code=503, detail="job queue is full")
    job_id = uuid.uuid4().hex
    safe_name = sanitize_name(job_name or Path(file.filename or "target").stem or job_id)
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    archive_path = job_dir / "input_archive.tar.gz"
    with archive_path.open("wb") as fp:
        shutil.copyfileobj(file.file, fp)
    spec = JobSpec(
        job_id=job_id,
        job_name=job_name or safe_name,
        safe_name=safe_name,
        jobs_dir=JOBS_DIR,
        docker_image=DOCKER_IMAGE,
        timeout_seconds=timeout_seconds,
        archive_path=archive_path,
    )
    write_json(job_dir / "request.json", {"job_id": job_id, "job_name": spec.job_name, "safe_name": safe_name, "filename": file.filename, "timeout_seconds": timeout_seconds})
    update_status(job_dir, job_id=job_id, status="queued", error=None, result_available=False)
    job_queue.put(spec)
    return {"job_id": job_id, "status": "queued", "job_name": spec.job_name, "safe_name": safe_name}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job_dir = JOBS_DIR / job_id
    status_path = job_dir / "status.json"
    if not status_path.is_file():
        raise HTTPException(status_code=404, detail="job not found")
    payload = read_json(status_path)
    payload["result_available"] = (job_dir / "result.json").is_file()
    return payload


@app.get("/jobs/{job_id}/result")
def get_result(job_id: str) -> dict[str, Any]:
    job_dir = JOBS_DIR / job_id
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        status_path = job_dir / "status.json"
        if status_path.is_file():
            status = read_json(status_path)
            raise HTTPException(status_code=409, detail={"status": status.get("status"), "error": status.get("error")})
        raise HTTPException(status_code=404, detail="job not found")
    return read_json(result_path)
