from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

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

app = FastAPI(title="TIVER source-level adaptive-version service", version="1.0.0")
job_queue: queue.Queue[str] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
job_specs: dict[str, JobSpec] = {}
job_lock = threading.Lock()
worker_started = False


class ScanRequest(BaseModel):
    target_path: str | None = Field(default=None, description="Absolute path to an existing source tree on the host")
    git_url: str | None = Field(default=None, description="Git URL to clone before scanning")
    git_ref: str | None = Field(default=None, description="Optional branch, tag, or commit to checkout after cloning")
    job_name: str | None = Field(default=None, description="Display name for the job. Underscores are converted internally.")
    timeout_seconds: int = Field(default=0, ge=0, description="0 means no timeout")

    @model_validator(mode="after")
    def validate_input(self) -> "ScanRequest":
        if bool(self.target_path) == bool(self.git_url):
            raise ValueError("provide exactly one of target_path or git_url")
        if self.git_ref and not self.git_url:
            raise ValueError("git_ref is only valid with git_url")
        return self


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


def make_spec(request: ScanRequest) -> JobSpec:
    job_id = uuid.uuid4().hex
    job_name = request.job_name or job_id
    safe_repo_name = sanitize_repo_name(job_name)
    target_path = require_absolute_dir(Path(request.target_path), "target_path") if request.target_path else None
    return JobSpec(
        job_id=job_id,
        job_name=job_name,
        safe_repo_name=safe_repo_name,
        jobs_dir=JOBS_DIR,
        docker_image=DOCKER_IMAGE,
        timeout_seconds=request.timeout_seconds,
        target_path=target_path,
        git_url=request.git_url,
        git_ref=request.git_ref,
    )


def worker_loop() -> None:
    while True:
        job_id = job_queue.get()
        with job_lock:
            spec = job_specs.get(job_id)
        if spec is None:
            write_status(job_id, {"job_id": job_id, "status": "failed", "error": "job specification was lost"})
            job_queue.task_done()
            continue
        try:
            run_tiver_job(spec)
        except Exception as exc:
            write_status(job_id, {"job_id": job_id, "status": "failed", "error": str(exc)})
        finally:
            job_queue.task_done()


@app.on_event("startup")
def start_worker() -> None:
    global worker_started
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if not worker_started:
        thread = threading.Thread(target=worker_loop, name="tiver-worker", daemon=True)
        thread.start()
        worker_started = True


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "docker_image": DOCKER_IMAGE,
        "jobs_dir": str(JOBS_DIR),
        "queued_jobs": job_queue.qsize(),
    }


@app.post("/scan", response_model=ScanResponse)
def scan(request: ScanRequest) -> ScanResponse:
    try:
        spec = make_spec(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        create_job_dirs(spec.job_dir)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to create job directory: {exc}") from exc

    write_json(
        spec.job_dir / "request.json",
        {
            "target_path": request.target_path,
            "git_url": request.git_url,
            "git_ref": request.git_ref,
            "job_name": request.job_name,
            "timeout_seconds": request.timeout_seconds,
        },
    )
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
