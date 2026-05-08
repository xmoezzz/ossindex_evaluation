from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from .hidx import parse_hidx_lines

DEFAULT_DATA_DIR = "data/ossdetector"


class HashEntry(BaseModel):
    hash: str
    path: Optional[str] = None
    paths: Optional[List[str]] = None


class DetectHashesRequest(BaseModel):
    input_name: str
    hashes: List[Union[str, HashEntry]] = Field(default_factory=list)
    include_vulnerabilities: bool = False


class VulnerabilityLookup(BaseModel):
    component: str
    version: str


class VulnerabilityBatchRequest(BaseModel):
    items: List[VulnerabilityLookup] = Field(default_factory=list)


class DetectorState:
    """Lazy detector holder.

    The HTTP server must start without scanning componentDB_ours_6.0. The
    OSSDetector database is required only when a detection request is actually
    executed, so the DataStore/Detector pair is created on the first detection
    request and reused by later requests in the same worker process.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser()
        # RLock is required because detector() calls store(), and both paths
        # guard lazy initialization with the same lock. A plain Lock deadlocks
        # on the first detection request.
        self._lock = threading.RLock()
        self._store = None
        self._detector = None
        self._status_lock = threading.Lock()
        self._init_status: Dict[str, object] = {
            "state": "not_started",
            "data_dir": str(self.data_dir),
            "updated_at": None,
        }

    def _update_init_status(self, event: Dict[str, object]) -> None:
        snapshot = dict(event)
        snapshot["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._status_lock:
            self._init_status.update(snapshot)
        stage = snapshot.get("stage", "init")
        message = snapshot.get("message", "")
        processed = snapshot.get("processed_files", snapshot.get("processed_rows"))
        total = snapshot.get("total_files", snapshot.get("total_rows"))
        percent = snapshot.get("percent")
        detail = ""
        if processed is not None and total is not None:
            detail = f" {processed}/{total}"
            if percent is not None:
                detail += f" ({percent}%)"
        extra_parts = []
        for key in ("components", "unique_hashes", "skipped_files", "loaded_rows"):
            value = snapshot.get(key)
            if value is not None:
                extra_parts.append(f"{key}={value}")
        extra = f" {' '.join(extra_parts)}" if extra_parts else ""
        print(f"[ossdetector:init] {stage}{detail} {message}{extra}", file=sys.stderr, flush=True)

    def init_status(self) -> Dict[str, object]:
        with self._status_lock:
            return dict(self._init_status)

    def store(self):  # intentionally unannotated to avoid startup imports
        if self._store is not None:
            return self._store
        with self._lock:
            if self._store is None:
                from .datastore import DataStore

                try:
                    self._update_init_status({"state": "initializing", "stage": "store", "message": "creating DataStore"})
                    self._store = DataStore(self.data_dir, progress_callback=self._update_init_status)
                    stats = self._store.stats()
                    self._update_init_status({"state": "ready", "stage": "ready", "message": "DataStore ready", **stats})
                except Exception as exc:
                    self._update_init_status({"state": "failed", "stage": "failed", "message": str(exc)})
                    raise
        return self._store

    def detector(self):  # intentionally unannotated to avoid startup imports
        if self._detector is not None:
            return self._detector
        with self._lock:
            if self._detector is None:
                from .detector import Detector

                self._detector = Detector(self.store())
        return self._detector


def normalize_hash_entries(entries: List[Union[str, HashEntry]]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for entry in entries:
        if isinstance(entry, str):
            result.setdefault(entry, []).append("")
            continue
        paths: List[str] = []
        if entry.path is not None:
            paths.append(entry.path)
        if entry.paths is not None:
            paths.extend(entry.paths)
        if not paths:
            paths.append("")
        result.setdefault(entry.hash, []).extend(paths)
    return result


def run_detection(
    state: DetectorState,
    input_name: str,
    hash_paths: Dict[str, List[str]],
    *,
    include_vulnerabilities: bool = False,
) -> Dict[str, object]:
    from .detector import DetectRequest

    detector = state.detector()
    result = detector.detect(
        DetectRequest(
            input_name=input_name,
            hashes=hash_paths,
            include_vulnerabilities=include_vulnerabilities,
        )
    )
    return result.to_dict()


def query_vulnerabilities(state: DetectorState, component: str, version: str) -> Dict[str, object]:
    store = state.store()
    repo_name = store.repo_name_from_component(component)
    vulnerabilities = [dict(item) for item in store.find_vulnerabilities(repo_name, version)]
    return {
        "component": repo_name,
        "version": version,
        "vulnerable": bool(vulnerabilities),
        "vulnerabilities": vulnerabilities,
    }


def create_app(data_dir: str | Path) -> FastAPI:
    state = DetectorState(data_dir)
    app = FastAPI(title="OSSDetector Service", version="1.1")

    @app.get("/health")
    def health() -> Dict[str, object]:
        return {"ok": True}

    @app.get("/init-status")
    def init_status() -> Dict[str, object]:
        return state.init_status()

    @app.get("/data-dir")
    def data_dir_info() -> Dict[str, object]:
        data_root = state.data_dir
        return {
            "data_dir": str(data_root),
            "exists": data_root.exists(),
            "cve_version_path": str(data_root / "cve-version.xlsx"),
            "cve_version_exists": (data_root / "cve-version.xlsx").is_file(),
        }

    @app.post("/detect/hashes")
    def detect_hashes(body: DetectHashesRequest) -> Dict[str, object]:
        hash_paths = normalize_hash_entries(body.hashes)
        try:
            return run_detection(
                state,
                body.input_name,
                hash_paths,
                include_vulnerabilities=body.include_vulnerabilities,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/detect/hidx")
    def detect_hidx(
        file: UploadFile = File(...),
        input_name: Optional[str] = Form(default=None),
        include_vulnerabilities: bool = Form(default=False),
    ) -> Dict[str, object]:
        try:
            content = file.file.read()
            if isinstance(content, str):
                text = content
            else:
                text = content.decode("utf-8", errors="replace")
            hash_paths = parse_hidx_lines(text.splitlines())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        effective_name = input_name or (file.filename or "input").split("_fuzzy", 1)[0]
        try:
            return run_detection(
                state,
                effective_name,
                hash_paths,
                include_vulnerabilities=include_vulnerabilities,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/vulns")
    def vulns(
        component: str = Query(...),
        version: str = Query(...),
    ) -> Dict[str, object]:
        try:
            return query_vulnerabilities(state, component, version)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/vulns/batch")
    def vulns_batch(body: VulnerabilityBatchRequest) -> Dict[str, object]:
        try:
            return {
                "items": [
                    query_vulnerabilities(state, item.component, item.version)
                    for item in body.items
                ]
            }
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


def create_app_from_env() -> FastAPI:
    data_dir = os.environ.get("OSSDETECTOR_DATA_DIR", DEFAULT_DATA_DIR)
    return create_app(data_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run OSSDetector as an HTTP service")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Directory containing OSSDetector data subdirectories")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    import uvicorn

    os.environ["OSSDETECTOR_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    uvicorn.run(
        "ossdetector_service.server:create_app_from_env",
        host=args.host,
        port=args.port,
        workers=args.workers,
        factory=True,
        app_dir=str(Path(__file__).resolve().parents[1]),
        reload=False,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
