#!/usr/bin/env python3
"""Local CENTRIS HTTP service.

This service wraps the original CENTRIS Detector.py as a Docker-based batch job.
It does not modify the CENTRIS detection algorithm.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import random
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

SERVICE_NAME = "centris"
SERVICE_VERSION = "1.0"

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
WORK_DIR = ROOT / "work"
OUTPUT_DIR = ROOT / "output"
EXTRACTED_DIR = DATA_DIR / "extracted"
RUNTIME_DIR = DATA_DIR / "runtime"
DATA_TAR = DATA_DIR / "Centris_dataset.tar"

IMAGE = os.environ.get("CENTRIS_IMAGE", "seunghoonwoo/centris_code:latest")
HOST = os.environ.get("CENTRIS_HOST", "0.0.0.0")
PORT = int(os.environ.get("CENTRIS_PORT", "5679"))
MAX_JOBS = int(os.environ.get("CENTRIS_MAX_JOBS", "1"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("CENTRIS_TIMEOUT_SECONDS", "3600"))

STATE_LOCK = threading.RLock()
JOB_SEMAPHORE = threading.Semaphore(MAX_JOBS)
JOBS: Dict[str, Dict[str, Any]] = {}
PROCESSES: Dict[str, subprocess.Popen] = {}
CANCELLED: set[str] = set()
DATA_LOCK = threading.RLock()


class ServiceError(Exception):
    """Expected service error that should be returned to the client."""


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in (DATA_DIR, WORK_DIR, OUTPUT_DIR, EXTRACTED_DIR, RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)


def safe_name(value: Optional[str], fallback: str = "job") -> str:
    if not value:
        value = fallback
    value = Path(str(value)).name
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    if not value:
        value = fallback
    return value[:80]


def make_job_id(job_name: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"{random.getrandbits(32):08x}"
    return f"{ts}_{safe_name(job_name)}_{suffix}"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length_header = handler.headers.get("Content-Length")
    if not length_header:
        return {}
    try:
        length = int(length_header)
    except ValueError as exc:
        raise ServiceError("Invalid Content-Length") from exc
    if length > 1024 * 1024:
        raise ServiceError("Request body is too large")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ServiceError(f"Invalid JSON body: {exc}") from exc


def send_json(handler: BaseHTTPRequestHandler, status: int, obj: Any) -> None:
    payload = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def update_job(job_id: str, **fields: Any) -> None:
    with STATE_LOCK:
        job = JOBS[job_id]
        job.update(fields)
        out_dir = OUTPUT_DIR / job_id
        write_json(out_dir / "meta.json", job)


def get_job(job_id: str) -> Dict[str, Any]:
    with STATE_LOCK:
        if job_id in JOBS:
            return dict(JOBS[job_id])
    meta_path = OUTPUT_DIR / job_id / "meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    raise ServiceError(f"Unknown job_id: {job_id}")


def find_dir(search_roots: List[Path], relative_candidates: List[str], leaf_name: str) -> Optional[Path]:
    for root in search_roots:
        for rel in relative_candidates:
            candidate = root / rel
            if candidate.is_dir():
                return candidate.resolve()
    for root in search_roots:
        if not root.exists():
            continue
        for candidate in root.rglob(leaf_name):
            if candidate.is_dir() and candidate.name == leaf_name and "runtime" not in candidate.parts:
                return candidate.resolve()
    return None


def reset_symlink_or_copy(link_path: Path, source_path: Path) -> None:
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink() or link_path.is_file():
            link_path.unlink()
        else:
            shutil.rmtree(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to(source_path, target_is_directory=True)
    except OSError:
        shutil.copytree(source_path, link_path)


def ensure_data_ready() -> Dict[str, str]:
    """Extract Centris_dataset.tar and build the runtime layout expected by Detector.py."""
    with DATA_LOCK:
        ensure_dirs()
        marker = RUNTIME_DIR / ".ready"
        required_runtime = [
            RUNTIME_DIR / "preprocessor" / "componentDB",
            RUNTIME_DIR / "preprocessor" / "verIDX",
            RUNTIME_DIR / "preprocessor" / "initialSigs",
            RUNTIME_DIR / "preprocessor" / "metaInfos",
            RUNTIME_DIR / "osscollector" / "repo_functions",
        ]
        if marker.exists() and all(path.exists() for path in required_runtime):
            return {
                "runtime": str(RUNTIME_DIR),
                "preprocessor": str(RUNTIME_DIR / "preprocessor"),
                "osscollector": str(RUNTIME_DIR / "osscollector"),
            }

        if not DATA_TAR.exists() and not EXTRACTED_DIR.exists():
            raise ServiceError(f"Missing dataset archive: {DATA_TAR}")

        extract_marker = EXTRACTED_DIR / ".extracted"
        if DATA_TAR.exists() and not extract_marker.exists():
            EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
            with tarfile.open(DATA_TAR, "r") as tf:
                tf.extractall(EXTRACTED_DIR)
            extract_marker.write_text(now_iso(), encoding="utf-8")

        search_roots = [EXTRACTED_DIR, DATA_DIR]

        component_db = find_dir(
            search_roots,
            ["preprocessor/componentDB", "componentDB", "Centris_dataset/preprocessor/componentDB", "Centris_dataset/componentDB"],
            "componentDB",
        )
        ver_idx = find_dir(
            search_roots,
            ["preprocessor/verIDX", "verIDX", "Centris_dataset/preprocessor/verIDX", "Centris_dataset/verIDX"],
            "verIDX",
        )
        initial_sigs = find_dir(
            search_roots,
            ["preprocessor/initialSigs", "initialSigs", "Centris_dataset/preprocessor/initialSigs", "Centris_dataset/initialSigs"],
            "initialSigs",
        )
        meta_infos = find_dir(
            search_roots,
            ["preprocessor/metaInfos", "metaInfos", "Centris_dataset/preprocessor/metaInfos", "Centris_dataset/metaInfos"],
            "metaInfos",
        )
        repo_functions = find_dir(
            search_roots,
            ["osscollector/repo_functions", "repo_functions", "Centris_dataset/osscollector/repo_functions", "Centris_dataset/repo_functions"],
            "repo_functions",
        )

        missing = []
        for name, path in [
            ("componentDB", component_db),
            ("verIDX", ver_idx),
            ("initialSigs", initial_sigs),
            ("metaInfos", meta_infos),
            ("repo_functions", repo_functions),
        ]:
            if path is None:
                missing.append(name)
        if missing:
            raise ServiceError(
                "Centris dataset is incomplete or has an unsupported layout. Missing: " + ", ".join(missing)
            )

        preprocessor_runtime = RUNTIME_DIR / "preprocessor"
        osscollector_runtime = RUNTIME_DIR / "osscollector"
        preprocessor_runtime.mkdir(parents=True, exist_ok=True)
        osscollector_runtime.mkdir(parents=True, exist_ok=True)

        reset_symlink_or_copy(preprocessor_runtime / "componentDB", component_db)  # type: ignore[arg-type]
        reset_symlink_or_copy(preprocessor_runtime / "verIDX", ver_idx)  # type: ignore[arg-type]
        reset_symlink_or_copy(preprocessor_runtime / "initialSigs", initial_sigs)  # type: ignore[arg-type]
        reset_symlink_or_copy(preprocessor_runtime / "metaInfos", meta_infos)  # type: ignore[arg-type]
        reset_symlink_or_copy(osscollector_runtime / "repo_functions", repo_functions)  # type: ignore[arg-type]

        marker.write_text(now_iso(), encoding="utf-8")
        return {
            "runtime": str(RUNTIME_DIR),
            "preprocessor": str(preprocessor_runtime),
            "osscollector": str(osscollector_runtime),
        }


def parse_raw_result(raw_path: Path) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not raw_path.exists():
        return results
    for line_no, line in enumerate(raw_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            results.append({"parse_error": "expected at least 7 tab-separated fields", "line_no": line_no, "raw_line": line})
            continue
        target, component, predicted_version, used, unused, modified, structure_changed = parts[:7]
        try:
            used_i = int(used)
            unused_i = int(unused)
            modified_i = int(modified)
        except ValueError:
            used_i = unused_i = modified_i = None  # type: ignore[assignment]
        results.append(
            {
                "kind": "component_origin",
                "target": target,
                "component": component,
                "predicted_version": predicted_version,
                "used_functions": used_i,
                "unused_functions": unused_i,
                "modified_functions": modified_i,
                "structure_changed": structure_changed.strip().lower() == "true",
                "raw_line": line,
            }
        )
    return results


def prepare_input(job_id: str, job_name: str, target_path: Path, input_kind: str) -> Path:
    if input_kind == "directory":
        if not target_path.is_dir():
            raise ServiceError(f"target_path is not a directory: {target_path}")
        return target_path.resolve()
    if input_kind == "file":
        if not target_path.is_file():
            raise ServiceError(f"target_path is not a file: {target_path}")
        input_dir = WORK_DIR / job_id / "input" / job_name
        input_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_path, input_dir / target_path.name)
        return input_dir.resolve()
    raise ServiceError("input_kind must be either 'directory' or 'file'")


def run_job(job_id: str) -> None:
    with STATE_LOCK:
        job = JOBS[job_id]
        target_path = Path(job["target_path"])
        input_kind = job["input_kind"]
        job_name = job["job_name"]
        timeout_seconds = int(job.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        keep_workdir = bool(job.get("keep_workdir", False))

    acquired = JOB_SEMAPHORE.acquire(blocking=True)
    if not acquired:
        update_job(job_id, status="failed", finished_at=now_iso(), error="Could not acquire job semaphore")
        return

    process: Optional[subprocess.Popen] = None
    try:
        if job_id in CANCELLED:
            update_job(job_id, status="cancelled", finished_at=now_iso(), error="Cancelled before start")
            return

        update_job(job_id, status="running", started_at=now_iso())
        data_info = ensure_data_ready()
        input_dir = prepare_input(job_id, job_name, target_path, input_kind)

        out_dir = OUTPUT_DIR / job_id
        res_dir = out_dir / "res"
        res_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = out_dir / "stdout.log"
        stderr_path = out_dir / "stderr.log"

        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{input_dir}:/input/{job_name}:ro",
            "-v",
            f"{data_info['preprocessor']}:/home/preprocessor:ro",
            "-v",
            f"{data_info['osscollector']}:/home/osscollector:ro",
            "-v",
            f"{res_dir}:/home/code/res",
            IMAGE,
            "/bin/bash",
            "-lc",
            f'cd /home/code && python3 Detector.py "/input/{job_name}"',
        ]

        write_json(out_dir / "command.json", {"command": command})

        with stdout_path.open("wb") as stdout_fp, stderr_path.open("wb") as stderr_fp:
            process = subprocess.Popen(command, stdout=stdout_fp, stderr=stderr_fp, start_new_session=True)
            with STATE_LOCK:
                PROCESSES[job_id] = process
            deadline = time.time() + timeout_seconds
            while True:
                rc = process.poll()
                if rc is not None:
                    break
                if job_id in CANCELLED:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    time.sleep(2)
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    update_job(job_id, status="cancelled", finished_at=now_iso(), error="Cancelled")
                    return
                if time.time() > deadline:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    time.sleep(2)
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    raise ServiceError(f"CENTRIS timed out after {timeout_seconds} seconds")
                time.sleep(0.5)

            if process.returncode != 0:
                raise ServiceError(f"CENTRIS Docker command failed with exit code {process.returncode}")

        raw_detector_result = res_dir / f"result_{job_name}"
        raw_tsv = out_dir / "raw_result.tsv"
        if raw_detector_result.exists():
            shutil.copy2(raw_detector_result, raw_tsv)
        else:
            raise ServiceError(f"CENTRIS did not produce expected result file: {raw_detector_result}")

        results = parse_raw_result(raw_tsv)
        result_obj = {
            "job_id": job_id,
            "service": SERVICE_NAME,
            "status": "done",
            "target_path": str(target_path),
            "input_kind": input_kind,
            "summary": {"result_count": len(results)},
            "results": results,
        }
        write_json(out_dir / "result.json", result_obj)
        update_job(job_id, status="done", finished_at=now_iso(), error=None)
    except Exception as exc:
        out_dir = OUTPUT_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        tb = traceback.format_exc()
        (out_dir / "exception.log").write_text(tb, encoding="utf-8")
        update_job(job_id, status="failed", finished_at=now_iso(), error=str(exc))
    finally:
        with STATE_LOCK:
            PROCESSES.pop(job_id, None)
        if not keep_workdir:
            shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
        JOB_SEMAPHORE.release()


def submit_scan(body: Dict[str, Any]) -> Dict[str, Any]:
    target_raw = body.get("target_path")
    if not target_raw:
        raise ServiceError("Missing required field: target_path")
    target_path = Path(str(target_raw)).expanduser()
    if not target_path.is_absolute():
        raise ServiceError("target_path must be an absolute path")

    input_kind = str(body.get("input_kind") or "directory").lower()
    if input_kind not in {"directory", "file"}:
        raise ServiceError("input_kind must be either 'directory' or 'file'")

    fallback = target_path.stem if input_kind == "file" else target_path.name
    job_name = safe_name(body.get("job_name"), fallback=fallback)
    job_id = make_job_id(job_name)
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise ServiceError("options must be an object")

    job = {
        "job_id": job_id,
        "job_name": job_name,
        "service": SERVICE_NAME,
        "status": "queued",
        "created_at": now_iso(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "target_path": str(target_path),
        "input_kind": input_kind,
        "timeout_seconds": int(options.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        "keep_workdir": bool(options.get("keep_workdir", False)),
    }

    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    with STATE_LOCK:
        JOBS[job_id] = job
        write_json(out_dir / "meta.json", job)

    thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "queued"}


def job_result(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    result_path = OUTPUT_DIR / job_id / "result.json"
    if result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    return {
        "job_id": job_id,
        "service": SERVICE_NAME,
        "status": job["status"],
        "error": job.get("error"),
        "results": [],
    }


def job_artifacts(job_id: str) -> Dict[str, Any]:
    get_job(job_id)
    out_dir = OUTPUT_DIR / job_id
    candidates = ["result.json", "raw_result.tsv", "stdout.log", "stderr.log", "meta.json", "command.json", "exception.log"]
    artifacts = []
    for name in candidates:
        path = out_dir / name
        if path.exists():
            artifacts.append({"name": name, "path": str(path), "size_bytes": path.stat().st_size})
    return {"job_id": job_id, "artifacts": artifacts}


def cancel_job(job_id: str) -> Dict[str, Any]:
    job = get_job(job_id)
    status = job.get("status")
    if status in {"done", "failed", "cancelled"}:
        return {"job_id": job_id, "status": status}
    CANCELLED.add(job_id)
    with STATE_LOCK:
        proc = PROCESSES.get(job_id)
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    update_job(job_id, status="cancelled", finished_at=now_iso(), error="Cancelled")
    return {"job_id": job_id, "status": "cancelled"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CentrisService/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/healthz":
                send_json(self, HTTPStatus.OK, {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION})
                return
            if path == "/capabilities":
                send_json(
                    self,
                    HTTPStatus.OK,
                    {
                        "service": SERVICE_NAME,
                        "version": SERVICE_VERSION,
                        "input_kinds": ["directory", "file"],
                        "supported_extensions": [".c", ".cc", ".cpp"],
                        "result_kind": "component_origin",
                        "async": True,
                    },
                )
                return
            m = re.fullmatch(r"/jobs/([^/]+)", path)
            if m:
                send_json(self, HTTPStatus.OK, get_job(m.group(1)))
                return
            m = re.fullmatch(r"/jobs/([^/]+)/result", path)
            if m:
                send_json(self, HTTPStatus.OK, job_result(m.group(1)))
                return
            m = re.fullmatch(r"/jobs/([^/]+)/artifacts", path)
            if m:
                send_json(self, HTTPStatus.OK, job_artifacts(m.group(1)))
                return
            if path == "/jobs":
                with STATE_LOCK:
                    jobs = list(JOBS.values())
                send_json(self, HTTPStatus.OK, {"jobs": jobs})
                return
            send_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ServiceError as exc:
            send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            send_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/scan":
                body = read_json_body(self)
                send_json(self, HTTPStatus.ACCEPTED, submit_scan(body))
                return
            m = re.fullmatch(r"/jobs/([^/]+)/cancel", path)
            if m:
                send_json(self, HTTPStatus.OK, cancel_job(m.group(1)))
                return
            send_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ServiceError as exc:
            send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            send_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


def main() -> None:
    ensure_dirs()
    print(f"Starting {SERVICE_NAME} service on http://{HOST}:{PORT}")
    print(f"Docker image: {IMAGE}")
    print(f"Dataset archive: {DATA_TAR}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
