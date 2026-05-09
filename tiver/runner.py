#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


OUTPUT_DIR_NAMES = [
    "res",
    "funcs",
    "output",
    "existPaths",
    "existPaths_v",
    "verPerHash",
]

InputKind = Literal["directory", "archive", "batch_archive"]


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    job_name: str
    safe_repo_name: str
    jobs_dir: Path
    docker_image: str
    timeout_seconds: int
    target_path: Path
    input_kind: InputKind = "directory"
    batch_manifest: list[dict[str, Any]] = field(default_factory=list)

    @property
    def job_dir(self) -> Path:
        return self.jobs_dir / self.job_id


def sanitize_repo_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.-]+", "-", value.strip())
    cleaned = cleaned.strip(".-")
    if not cleaned:
        raise ValueError("job_name must contain at least one alphanumeric character")
    if "_" in cleaned:
        raise ValueError("internal error: sanitized repository name still contains an underscore")
    return cleaned[:80]


def require_absolute_dir(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {path}")
    if not path.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")
    return path.resolve()


def ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required executable not found in PATH: {name}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_status(job_dir: Path, **updates: Any) -> None:
    status_path = job_dir / "status.json"
    if status_path.exists():
        payload = read_json(status_path)
    else:
        payload = {}
    payload.update(updates)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(status_path, payload)


def create_job_dirs(job_dir: Path) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_DIR_NAMES:
        (job_dir / name).mkdir(parents=True, exist_ok=True)


def _worker_name(jobs_dir: Path, docker_image: str) -> str:
    raw = f"{jobs_dir.resolve()}::{docker_image}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"tiver-worker-{digest}"


_WORKER_LOCK = threading.Lock()
_WORKER_STARTED: dict[str, bool] = {}
_WORKER_CLEANUP_REGISTERED = False


def _all_known_workers() -> list[str]:
    return list(_WORKER_STARTED.keys())


def _cleanup_workers_at_exit() -> None:
    for name in _all_known_workers():
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def ensure_worker_container(jobs_dir: Path, docker_image: str) -> str:
    global _WORKER_CLEANUP_REGISTERED
    ensure_tool("docker")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    name = _worker_name(jobs_dir, docker_image)
    with _WORKER_LOCK:
        if not _WORKER_CLEANUP_REGISTERED:
            atexit.register(_cleanup_workers_at_exit)
            _WORKER_CLEANUP_REGISTERED = True
        if _WORKER_STARTED.get(name):
            return name
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            "PYTHONUNBUFFERED=1",
            "-v",
            f"{jobs_dir}:/jobs",
            docker_image,
            "tail",
            "-f",
            "/dev/null",
        ]
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"failed to start TIVER worker container {name}: {completed.stderr.strip()}")
        _WORKER_STARTED[name] = True
        return name


def reset_worker_container(jobs_dir: Path, docker_image: str) -> None:
    name = _worker_name(jobs_dir, docker_image)
    with _WORKER_LOCK:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        _WORKER_STARTED.pop(name, None)


def run_command(
    argv: list[str],
    *,
    cwd: Path | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> int:
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        stdout.write(("$ " + " ".join(argv) + "\n").encode("utf-8", errors="replace"))
        stdout.flush()
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd else None,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds if timeout_seconds > 0 else None,
                check=False,
            )
            return completed.returncode
        except subprocess.TimeoutExpired:
            stderr.write(f"command timed out after {timeout_seconds} seconds\n".encode("utf-8"))
            stderr.flush()
            return 124


def _copy_source_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def prepare_worker_input(spec: JobSpec) -> Path:
    job_dir = spec.job_dir
    worker_input = job_dir / "worker_input"
    if worker_input.exists():
        shutil.rmtree(worker_input)
    worker_input.mkdir(parents=True, exist_ok=False)

    if spec.input_kind == "batch_archive":
        batch_source = spec.target_path
        if not batch_source.is_dir():
            raise RuntimeError(f"batch source directory not found: {batch_source}")
        top_dirs = [p for p in sorted(batch_source.iterdir(), key=lambda p: p.name) if p.is_dir()]
        if not top_dirs:
            raise RuntimeError("batch archive contains no top-level source directories")
        for src in top_dirs:
            safe_name = sanitize_repo_name(src.name)
            if safe_name != src.name:
                raise RuntimeError(f"batch source directory name is not sanitized: {src.name}")
            _copy_source_tree(src, worker_input / safe_name)
        return worker_input

    if not spec.target_path.is_dir():
        raise RuntimeError(f"source directory not found: {spec.target_path}")
    _copy_source_tree(spec.target_path, worker_input / spec.safe_repo_name)
    return worker_input


def worker_exec_command(spec: JobSpec, worker_name: str) -> list[str]:
    container_script = r'''
set -euo pipefail
JOB_ID="${TIVER_JOB_ID:?missing TIVER_JOB_ID}"
cd /tiver/tiver_public

rm -rf /tiver/clonehere res funcs output existPaths existPaths_v verPerHash
mkdir -p /tiver/clonehere res funcs output existPaths existPaths_v verPerHash
cp -a "/jobs/${JOB_ID}/worker_input/." /tiver/clonehere/

echo "[TIVER_STAGE] job=${TIVER_JOB_ID:-unknown} repo=${TIVER_REPO_NAME:-unknown} input_kind=${TIVER_INPUT_KIND:-unknown} start $(date -Is)"
echo "[TIVER_STAGE] pwd=$(pwd)"
echo "[TIVER_STAGE] clonehere contents:"
find /tiver/clonehere -maxdepth 2 -mindepth 1 -printf '%y %p\n' 2>/dev/null | sort | head -500 || true
echo "[TIVER_STAGE] source file sample:"
find /tiver/clonehere -type f \( -name '*.c' -o -name '*.cc' -o -name '*.cpp' -o -name '*.cxx' -o -name '*.h' -o -name '*.hpp' \) -printf '%p\n' 2>/dev/null | head -200 || true

run_stage() {
    stage="$1"
    shift
    echo "[TIVER_STAGE] start ${stage} $(date -Is)"
    set +e
    "$@" 2>&1
    rc=$?
    set -e
    echo "[TIVER_STAGE] done ${stage} rc=${rc} $(date -Is)"
    echo "[TIVER_STAGE] artifact counts after ${stage}:"
    for d in res funcs output existPaths existPaths_v verPerHash; do
        count=$(find "$d" -type f 2>/dev/null | wc -l | tr -d ' ')
        size=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
        echo "[TIVER_STAGE]   ${d}: files=${count} size=${size:-0}"
    done
    return "$rc"
}

run_stage Centris_multi python3 -u Centris_multi.py 0 linux
run_stage tarParser python3 -u tarParser.py
run_stage tiver python3 -u tiver.py

for d in res funcs output existPaths existPaths_v verPerHash; do
    rm -rf "/jobs/${JOB_ID}/${d}"
    cp -a "$d" "/jobs/${JOB_ID}/${d}"
done

echo "[TIVER_STAGE] job=${TIVER_JOB_ID:-unknown} repo=${TIVER_REPO_NAME:-unknown} finished $(date -Is)"
rm -rf /tiver/clonehere res funcs output existPaths existPaths_v verPerHash
'''.strip()
    return [
        "docker",
        "exec",
        "-e",
        f"TIVER_JOB_ID={spec.job_id}",
        "-e",
        f"TIVER_REPO_NAME={spec.safe_repo_name}",
        "-e",
        f"TIVER_INPUT_KIND={spec.input_kind}",
        worker_name,
        "bash",
        "-lc",
        container_script,
    ]


def parse_section_file(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        if not lines[i]:
            i += 1
            continue
        name = lines[i]
        i += 1
        while i < len(lines) and not lines[i]:
            i += 1
        if i >= len(lines):
            result[name] = None
            break
        raw_json = lines[i]
        i += 1
        try:
            result[name] = json.loads(raw_json)
        except json.JSONDecodeError:
            result[name] = {"parse_error": raw_json}
    return result


def summarize_onevpf(onevpf: dict[str, Any]) -> dict[str, Any]:
    components: dict[str, Any] = {}
    for component, files in onevpf.items():
        version_counts: dict[str, int] = {}
        file_count = 0
        if isinstance(files, dict):
            file_count = len(files)
            for value in files.values():
                if isinstance(value, list) and value:
                    version_value = str(value[0])
                    version_counts[version_value] = version_counts.get(version_value, 0) + 1
        prevalent_version = None
        if version_counts:
            prevalent_version = sorted(version_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        components[component] = {
            "file_count": file_count,
            "prevalent_version_by_file_count": prevalent_version,
            "version_counts_by_file": version_counts,
            "files": files,
        }
    return components


def _artifact_record(job_dir: Path, file_path: Path) -> dict[str, Any]:
    return {"path": str(file_path.relative_to(job_dir)), "size_bytes": file_path.stat().st_size}


def collect_artifacts(job_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for dirname in OUTPUT_DIR_NAMES:
        root = job_dir / dirname
        if not root.exists():
            continue
        for file_path in root.rglob("*"):
            if file_path.is_file():
                artifacts.append(_artifact_record(job_dir, file_path))
    for filename in ["stdout.log", "stderr.log", "command.json", "meta.json", "status.json", "result.json", "request.json", "batch_manifest.json"]:
        file_path = job_dir / filename
        if file_path.exists():
            artifacts.append(_artifact_record(job_dir, file_path))
    return sorted(artifacts, key=lambda item: item["path"])


def _files_for_repo(job_dir: Path, dirname: str, repo_name: str, suffix: str) -> list[Path]:
    root = job_dir / dirname
    if not root.exists():
        return []
    exact = root / f"{repo_name}_{suffix}.txt"
    if exact.exists():
        return [exact]
    return sorted(root.glob(f"{repo_name}_*{suffix}.txt"))


def collect_repo_artifacts(job_dir: Path, repo_name: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    patterns = {
        "res": [f"{repo_name}_res.txt"],
        "funcs": [f"{repo_name}_funcs.txt"],
        "output": [f"{repo_name}_output.txt"],
        "existPaths": [f"{repo_name}_ep.txt"],
        "existPaths_v": [f"{repo_name}_epv.txt", f"{repo_name}_onevpf.txt"],
        "verPerHash": [f"{repo_name}_vph.txt"],
    }
    for dirname, names in patterns.items():
        for name in names:
            path = job_dir / dirname / name
            if path.is_file():
                artifacts.append(_artifact_record(job_dir, path))
    return sorted(artifacts, key=lambda item: item["path"])


def build_result_for_repo(job_dir: Path, repo_name: str) -> dict[str, Any]:
    onevpf_sections: dict[str, Any] = {}
    epv_sections: dict[str, Any] = {}
    vph_sections: dict[str, Any] = {}

    for path in _files_for_repo(job_dir, "existPaths_v", repo_name, "onevpf"):
        onevpf_sections.update(parse_section_file(path))
    for path in _files_for_repo(job_dir, "existPaths_v", repo_name, "epv"):
        epv_sections.update(parse_section_file(path))
    for path in _files_for_repo(job_dir, "verPerHash", repo_name, "vph"):
        vph_sections.update(parse_section_file(path))

    return {
        "safe_repo_name": repo_name,
        "component_count": len(onevpf_sections),
        "components": summarize_onevpf(onevpf_sections),
        "raw": {
            "onevpf": onevpf_sections,
            "epv": epv_sections,
            "verPerHash": vph_sections,
        },
        "artifacts": collect_repo_artifacts(job_dir, repo_name),
    }


def build_result(job_dir: Path, safe_repo_name: str) -> dict[str, Any]:
    meta_path = job_dir / "meta.json"
    meta = read_json(meta_path) if meta_path.exists() else {}
    if meta.get("input_kind") == "batch_archive":
        manifest_path = job_dir / "batch_manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else []
        repositories: dict[str, Any] = {}
        if isinstance(manifest, list):
            repo_names = [entry.get("repo_name") for entry in manifest if isinstance(entry, dict) and isinstance(entry.get("repo_name"), str)]
        else:
            repo_names = []
        if not repo_names:
            input_root = job_dir / "worker_input"
            repo_names = [p.name for p in sorted(input_root.iterdir(), key=lambda p: p.name) if p.is_dir()] if input_root.exists() else []
        for repo_name in repo_names:
            repositories[repo_name] = build_result_for_repo(job_dir, repo_name)
        return {
            "safe_repo_name": safe_repo_name,
            "input_kind": "batch_archive",
            "repository_count": len(repositories),
            "repositories": repositories,
            "artifacts": collect_artifacts(job_dir),
        }
    return build_result_for_repo(job_dir, safe_repo_name) | {"artifacts": collect_artifacts(job_dir)}


def run_tiver_job(spec: JobSpec) -> dict[str, Any]:
    ensure_tool("docker")
    create_job_dirs(spec.job_dir)

    stdout_path = spec.job_dir / "stdout.log"
    stderr_path = spec.job_dir / "stderr.log"
    meta = {
        "job_id": spec.job_id,
        "job_name": spec.job_name,
        "safe_repo_name": spec.safe_repo_name,
        "docker_image": spec.docker_image,
        "timeout_seconds": spec.timeout_seconds,
        "target_path": str(spec.target_path),
        "input_kind": spec.input_kind,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(spec.job_dir / "meta.json", meta)
    append_status(spec.job_dir, job_id=spec.job_id, status="running", error=None)

    try:
        prepare_worker_input(spec)
        worker_name = ensure_worker_container(spec.jobs_dir, spec.docker_image)
        cmd = worker_exec_command(spec, worker_name)
        write_json(spec.job_dir / "command.json", {"argv": cmd, "worker_container": worker_name})
        rc = run_command(
            cmd,
            cwd=None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=spec.timeout_seconds,
        )
        if rc == 124:
            reset_worker_container(spec.jobs_dir, spec.docker_image)
            raise RuntimeError(f"TIVER worker command timed out after {spec.timeout_seconds} seconds")
        if rc != 0:
            raise RuntimeError(f"TIVER worker command failed with exit code {rc}")

        result = build_result(spec.job_dir, spec.safe_repo_name)
        write_json(spec.job_dir / "result.json", result)
        append_status(spec.job_dir, status="succeeded", error=None)
        return result
    except Exception as exc:
        append_status(spec.job_dir, status="failed", error=str(exc))
        raise


def make_spec_from_args(args: argparse.Namespace) -> JobSpec:
    jobs_dir = Path(args.jobs_dir).expanduser().resolve()
    job_id = args.job_id or uuid.uuid4().hex
    job_name = args.job_name or job_id
    safe_repo_name = sanitize_repo_name(job_name)
    target_path = require_absolute_dir(Path(args.target_path), "target path")
    return JobSpec(
        job_id=job_id,
        job_name=job_name,
        safe_repo_name=safe_repo_name,
        jobs_dir=jobs_dir,
        docker_image=args.docker_image,
        timeout_seconds=args.timeout_seconds,
        target_path=target_path,
        input_kind="directory",
    )


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated TIVER job.")
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--jobs-dir", default="jobs")
    parser.add_argument("--docker-image", default="geniuschoi/tiver:latest")
    parser.add_argument("--timeout-seconds", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        spec = make_spec_from_args(args)
        result = run_tiver_job(spec)
        print(json.dumps({"job_id": spec.job_id, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
