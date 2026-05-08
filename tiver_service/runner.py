#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OUTPUT_DIR_NAMES = [
    "res",
    "funcs",
    "output",
    "existPaths",
    "existPaths_v",
    "verPerHash",
]


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    job_name: str
    safe_repo_name: str
    jobs_dir: Path
    docker_image: str
    timeout_seconds: int
    target_path: Path | None = None
    git_url: str | None = None
    git_ref: str | None = None

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


def write_json(path: Path, payload: Any) -> None:
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
    (job_dir / "clonehere").mkdir(parents=True, exist_ok=True)


def clone_repository(spec: JobSpec, stdout_path: Path, stderr_path: Path) -> Path:
    assert spec.git_url is not None
    ensure_tool("git")
    destination = spec.job_dir / "clonehere" / spec.safe_repo_name
    argv = ["git", "clone", "--recursive", spec.git_url, str(destination)]
    rc = run_command(argv, cwd=None, stdout_path=stdout_path, stderr_path=stderr_path, timeout_seconds=spec.timeout_seconds)
    if rc != 0:
        raise RuntimeError(f"git clone failed with exit code {rc}")
    if spec.git_ref:
        rc = run_command(
            ["git", "checkout", spec.git_ref],
            cwd=destination,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=spec.timeout_seconds,
        )
        if rc != 0:
            raise RuntimeError(f"git checkout failed with exit code {rc}")
        rc = run_command(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=destination,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=spec.timeout_seconds,
        )
        if rc != 0:
            raise RuntimeError(f"git submodule update failed with exit code {rc}")
    return destination


def docker_command_for_local_path(spec: JobSpec, source_path: Path) -> list[str]:
    job_dir = spec.job_dir
    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"tiver-{spec.job_id}",
        "-v",
        f"{source_path}:/tiver/clonehere/{spec.safe_repo_name}:ro",
    ]
    for name in OUTPUT_DIR_NAMES:
        cmd.extend(["-v", f"{job_dir / name}:/tiver/tiver_public/{name}"])
    cmd.extend(
        [
            spec.docker_image,
            "bash",
            "-lc",
            (
                "set -euo pipefail; "
                "cd /tiver/tiver_public; "
                "mkdir -p res funcs output existPaths existPaths_v verPerHash; "
                "python3 Centris_multi.py 0 linux; "
                "python3 tarParser.py; "
                "python3 tiver.py"
            ),
        ]
    )
    return cmd


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


def collect_artifacts(job_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for dirname in OUTPUT_DIR_NAMES + ["clonehere"]:
        root = job_dir / dirname
        if not root.exists():
            continue
        for file_path in root.rglob("*"):
            if file_path.is_file():
                artifacts.append(
                    {
                        "path": str(file_path.relative_to(job_dir)),
                        "size_bytes": file_path.stat().st_size,
                    }
                )
    for filename in ["stdout.log", "stderr.log", "command.json", "meta.json", "status.json", "result.json"]:
        file_path = job_dir / filename
        if file_path.exists():
            artifacts.append({"path": filename, "size_bytes": file_path.stat().st_size})
    return sorted(artifacts, key=lambda item: item["path"])


def build_result(job_dir: Path, safe_repo_name: str) -> dict[str, Any]:
    onevpf_files = sorted((job_dir / "existPaths_v").glob("*_onevpf.txt"))
    epv_files = sorted((job_dir / "existPaths_v").glob("*_epv.txt"))
    vph_files = sorted((job_dir / "verPerHash").glob("*_vph.txt"))

    onevpf_sections: dict[str, Any] = {}
    epv_sections: dict[str, Any] = {}
    vph_sections: dict[str, Any] = {}

    for path in onevpf_files:
        onevpf_sections.update(parse_section_file(path))
    for path in epv_files:
        epv_sections.update(parse_section_file(path))
    for path in vph_files:
        vph_sections.update(parse_section_file(path))

    return {
        "safe_repo_name": safe_repo_name,
        "component_count": len(onevpf_sections),
        "components": summarize_onevpf(onevpf_sections),
        "raw": {
            "onevpf": onevpf_sections,
            "epv": epv_sections,
            "verPerHash": vph_sections,
        },
        "artifacts": collect_artifacts(job_dir),
    }


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
        "target_path": str(spec.target_path) if spec.target_path else None,
        "git_url": spec.git_url,
        "git_ref": spec.git_ref,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(spec.job_dir / "meta.json", meta)
    append_status(spec.job_dir, job_id=spec.job_id, status="running", error=None)

    try:
        if spec.git_url:
            source_path = clone_repository(spec, stdout_path, stderr_path)
        else:
            assert spec.target_path is not None
            source_path = spec.target_path

        cmd = docker_command_for_local_path(spec, source_path)
        write_json(spec.job_dir / "command.json", {"argv": cmd})
        rc = run_command(
            cmd,
            cwd=None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=spec.timeout_seconds,
        )
        if rc != 0:
            raise RuntimeError(f"TIVER docker run failed with exit code {rc}")

        result = build_result(spec.job_dir, spec.safe_repo_name)
        write_json(spec.job_dir / "result.json", result)
        append_status(spec.job_dir, status="succeeded", error=None)
        return result
    except Exception as exc:
        append_status(spec.job_dir, status="failed", error=str(exc))
        raise


def make_spec_from_args(args: argparse.Namespace) -> JobSpec:
    if bool(args.target_path) == bool(args.git_url):
        raise ValueError("provide exactly one of --target-path or --git-url")
    jobs_dir = Path(args.jobs_dir).expanduser().resolve()
    job_id = args.job_id or uuid.uuid4().hex
    job_name = args.job_name or job_id
    safe_repo_name = sanitize_repo_name(job_name)
    target_path = require_absolute_dir(Path(args.target_path), "target path") if args.target_path else None
    return JobSpec(
        job_id=job_id,
        job_name=job_name,
        safe_repo_name=safe_repo_name,
        jobs_dir=jobs_dir,
        docker_image=args.docker_image,
        timeout_seconds=args.timeout_seconds,
        target_path=target_path,
        git_url=args.git_url,
        git_ref=args.git_ref,
    )


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated TIVER job.")
    parser.add_argument("--target-path")
    parser.add_argument("--git-url")
    parser.add_argument("--git-ref")
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
