from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from runner import (
    JobSpec,
    build_result,
    collect_artifacts,
    create_job_dirs,
    read_json,
    require_absolute_dir,
    run_tiver_job,
    sanitize_repo_name,
    write_json,
)


JOBS_DIR = Path(os.environ.get("TIVER_JOBS_DIR", "jobs")).expanduser().resolve()
DOCKER_IMAGE = os.environ.get("TIVER_DOCKER_IMAGE", "geniuschoi/tiver:latest")
MAX_QUEUE_SIZE = int(os.environ.get("TIVER_MAX_QUEUE_SIZE", "64"))
SERVER_WORKERS = int(os.environ.get("TIVER_SERVER_WORKERS", "1"))
if SERVER_WORKERS <= 0:
    raise RuntimeError("TIVER_SERVER_WORKERS must be positive")

app = FastAPI(title="TIVER source-level adaptive-version service", version="1.2.0")
job_queue: queue.Queue[str] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
job_specs: dict[str, JobSpec] = {}
job_lock = threading.Lock()
workers_started = False


class ScanRequest(BaseModel):
    target_path: str = Field(..., description="Absolute path to an existing source tree on the service host")
    job_name: str | None = Field(default=None, description="Display name for the job")
    timeout_seconds: int = Field(default=0, ge=0, description="0 means no timeout")


class ScanResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    safe_repo_name: str


class JobStatus(BaseModel):
    job_id: str
    status: str
    safe_repo_name: str | None = None
    job_name: str | None = None
    error: str | None = None
    updated_at: str | None = None
    result_available: bool = False


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def status_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "status.json"


def result_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "result.json"


def meta_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "meta.json"


def write_status(job_id: str, payload: dict[str, Any]) -> None:
    path = status_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = {}
    if path.exists():
        base = read_json(path)
    base.update(payload)
    base["updated_at"] = utc_now()
    write_json(path, base)


def load_status(job_id: str) -> dict[str, Any]:
    path = status_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="job not found")
    payload = read_json(path)
    payload["result_available"] = result_path(job_id).exists()
    if meta_path(job_id).exists():
        meta = read_json(meta_path(job_id))
        payload.setdefault("safe_repo_name", meta.get("safe_repo_name"))
        payload.setdefault("job_name", meta.get("job_name"))
    return payload


def _validate_timeout_seconds(value: int) -> int:
    if value < 0:
        raise ValueError("timeout_seconds must be non-negative")
    return value


def _save_upload_file(upload: UploadFile, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    size = 0
    try:
        with tmp.open("wb") as out:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
        tmp.replace(destination)
        return size
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def _load_manifest_json(manifest_json: str | None) -> list[dict[str, Any]]:
    if manifest_json is None or not manifest_json.strip():
        return []
    try:
        value = json.loads(manifest_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest_json is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("manifest_json must be a JSON list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"manifest item #{idx} is not an object")
        repo_name = item.get("repo_name")
        if not isinstance(repo_name, str) or not repo_name:
            raise ValueError(f"manifest item #{idx} has missing repo_name")
        safe = sanitize_repo_name(repo_name)
        if safe != repo_name:
            raise ValueError(f"manifest item #{idx} repo_name is not sanitized: {repo_name}")
        if repo_name in seen:
            raise ValueError(f"duplicate repo_name in manifest: {repo_name}")
        seen.add(repo_name)
        out.append(dict(item))
    return out


def make_directory_spec(request: ScanRequest) -> JobSpec:
    job_id = uuid.uuid4().hex
    job_name = request.job_name or job_id
    safe_repo_name = sanitize_repo_name(job_name)
    target_path = require_absolute_dir(Path(request.target_path), "target_path")
    return JobSpec(
        job_id=job_id,
        job_name=job_name,
        safe_repo_name=safe_repo_name,
        jobs_dir=JOBS_DIR,
        docker_image=DOCKER_IMAGE,
        timeout_seconds=request.timeout_seconds,
        target_path=target_path,
        input_kind="directory",
    )


def make_uploaded_spec(*, job_name_value: str | None, timeout_seconds_value: int) -> JobSpec:
    job_id = uuid.uuid4().hex
    job_name = job_name_value or job_id
    safe_repo_name = sanitize_repo_name(job_name)
    return JobSpec(
        job_id=job_id,
        job_name=job_name,
        safe_repo_name=safe_repo_name,
        jobs_dir=JOBS_DIR,
        docker_image=DOCKER_IMAGE,
        timeout_seconds=_validate_timeout_seconds(timeout_seconds_value),
        target_path=JOBS_DIR / job_id / "input_archive.tar.gz",
        input_kind="archive",
    )


def make_batch_spec(*, batch_name_value: str | None, timeout_seconds_value: int, manifest: list[dict[str, Any]]) -> JobSpec:
    job_id = uuid.uuid4().hex
    job_name = batch_name_value or f"batch-{job_id}"
    safe_repo_name = sanitize_repo_name(job_name)
    return JobSpec(
        job_id=job_id,
        job_name=job_name,
        safe_repo_name=safe_repo_name,
        jobs_dir=JOBS_DIR,
        docker_image=DOCKER_IMAGE,
        timeout_seconds=_validate_timeout_seconds(timeout_seconds_value),
        target_path=JOBS_DIR / job_id / "input_batch.tar.gz",
        input_kind="batch_archive",
        batch_manifest=manifest,
    )


def enqueue_spec(spec: JobSpec) -> ScanResponse:
    write_status(
        spec.job_id,
        {
            "job_id": spec.job_id,
            "status": "queued",
            "safe_repo_name": spec.safe_repo_name,
            "job_name": spec.job_name,
            "error": None,
        },
    )
    with job_lock:
        job_specs[spec.job_id] = spec
    try:
        job_queue.put_nowait(spec.job_id)
    except queue.Full as exc:
        write_status(spec.job_id, {"status": "failed", "error": "queue is full"})
        raise HTTPException(status_code=503, detail="queue is full") from exc
    return ScanResponse(job_id=spec.job_id, status="queued", safe_repo_name=spec.safe_repo_name)


def worker_loop(worker_index: int) -> None:
    while True:
        job_id = job_queue.get()
        with job_lock:
            spec = job_specs.get(job_id)
        if spec is None:
            write_status(job_id, {"job_id": job_id, "status": "failed", "error": "job specification was lost"})
            job_queue.task_done()
            continue
        try:
            run_tiver_job(spec, worker_slot=worker_index)
        except Exception as exc:
            write_status(job_id, {"job_id": job_id, "status": "failed", "error": str(exc)})
        finally:
            job_queue.task_done()


@app.on_event("startup")
def start_workers() -> None:
    global workers_started
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if not workers_started:
        for index in range(SERVER_WORKERS):
            thread = threading.Thread(target=worker_loop, args=(index,), name=f"tiver-worker-{index}", daemon=True)
            thread.start()
        workers_started = True


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "docker_image": DOCKER_IMAGE,
        "jobs_dir": str(JOBS_DIR),
        "queued_jobs": job_queue.qsize(),
        "server_workers": SERVER_WORKERS,
    }


@app.post("/scan", response_model=ScanResponse)
def scan(request: ScanRequest) -> ScanResponse:
    try:
        spec = make_directory_spec(request)
        create_job_dirs(spec.job_dir)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_json(
        spec.job_dir / "request.json",
        {
            "input_kind": "directory",
            "target_path": request.target_path,
            "job_name": request.job_name,
            "timeout_seconds": request.timeout_seconds,
        },
    )
    return enqueue_spec(spec)


@app.post("/scan_archive", response_model=ScanResponse)
def scan_archive(
    file: UploadFile = File(...),
    job_name: str | None = Form(default=None),
    timeout_seconds: int = Form(default=0),
) -> ScanResponse:
    try:
        spec = make_uploaded_spec(job_name_value=job_name, timeout_seconds_value=timeout_seconds)
        create_job_dirs(spec.job_dir)
        size = _save_upload_file(file, spec.target_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to save uploaded source archive: {exc}") from exc
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    write_json(
        spec.job_dir / "request.json",
        {
            "input_kind": "archive",
            "filename": file.filename,
            "archive_path": str(spec.target_path),
            "archive_size_bytes": size,
            "job_name": job_name,
            "timeout_seconds": timeout_seconds,
        },
    )
    return enqueue_spec(spec)


@app.post("/scan_batch_archive", response_model=ScanResponse)
def scan_batch_archive(
    file: UploadFile = File(...),
    batch_name: str | None = Form(default=None),
    timeout_seconds: int = Form(default=0),
    manifest_json: str | None = Form(default=None),
) -> ScanResponse:
    try:
        manifest = _load_manifest_json(manifest_json)
        spec = make_batch_spec(batch_name_value=batch_name, timeout_seconds_value=timeout_seconds, manifest=manifest)
        create_job_dirs(spec.job_dir)
        size = _save_upload_file(file, spec.target_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to save uploaded batch archive: {exc}") from exc
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    write_json(
        spec.job_dir / "request.json",
        {
            "input_kind": "batch_archive",
            "filename": file.filename,
            "archive_path": str(spec.target_path),
            "archive_size_bytes": size,
            "batch_name": batch_name,
            "timeout_seconds": timeout_seconds,
            "repository_count": len(manifest),
        },
    )
    if manifest:
        write_json(spec.job_dir / "batch_manifest.json", manifest)
    return enqueue_spec(spec)


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    return JobStatus(**load_status(job_id))


@app.get("/jobs/{job_id}/result")
def get_result(job_id: str) -> dict[str, Any]:
    path = result_path(job_id)
    if path.exists():
        return read_json(path)
    status = load_status(job_id)
    if status.get("status") == "succeeded":
        meta = read_json(meta_path(job_id))
        result = build_result(JOBS_DIR / job_id, meta["safe_repo_name"])
        write_json(path, result)
        return result
    raise HTTPException(status_code=404, detail="result is not available")


@app.get("/jobs/{job_id}/artifacts")
def get_artifacts(job_id: str) -> dict[str, Any]:
    load_status(job_id)
    return {"job_id": job_id, "artifacts": collect_artifacts(JOBS_DIR / job_id)}


@app.get("/jobs/{job_id}/artifacts/{artifact_path:path}")
def download_artifact(job_id: str, artifact_path: str) -> FileResponse:
    load_status(job_id)
    root = (JOBS_DIR / job_id).resolve()
    target = (root / artifact_path).resolve()
    if not str(target).startswith(str(root) + os.sep):
        raise HTTPException(status_code=400, detail="invalid artifact path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(target)
