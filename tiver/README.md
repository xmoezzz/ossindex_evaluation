# tiver_service

A local HTTP wrapper for TIVER. It does not change TIVER's algorithms. Each scan runs the official `geniuschoi/tiver:latest` Docker image in an isolated job directory and collects TIVER's raw outputs.

## What this service does

Input:

- an absolute source directory on the host, or
- a Git URL plus an optional branch, tag, or commit.

Execution per job:

```text
Centris_multi.py 0 linux
    -> res/
tarParser.py
    -> funcs/
tiver.py
    -> output/, existPaths/, existPaths_v/, verPerHash/
```

Important: TIVER is source-level C/C++ OSS adaptive-version identification. It is not a binary scanner.

```bash
python3 -m pip install -r requirements.txt
chmod +x run_service.sh run_tiver_job.sh

TIVER_JOBS_DIR=/data/tiver_service/jobs ./run_service.sh
```

## Requirements

- Linux host with Docker installed
- Python 3.10+
- Python packages from `requirements.txt`
- Docker image: `geniuschoi/tiver:latest`

Install Python deps:

```bash
python3 -m pip install -r requirements.txt
```

`run_service.sh` checks whether the configured Docker image exists locally. If it is missing, the script pulls it once before starting the HTTP service:

```bash
./run_service.sh
```

You may still pre-pull it manually when you want to control network timing:

```bash
docker pull geniuschoi/tiver:latest
```

## Start service

```bash
./run_service.sh
```

Defaults:

```text
host: 0.0.0.0
port: 5681
jobs dir: ./jobs
image: geniuschoi/tiver:latest
```

Override with env vars:

```bash
TIVER_PORT=5681 \
TIVER_JOBS_DIR=/data/tiver_service/jobs \
TIVER_DOCKER_IMAGE=geniuschoi/tiver:latest \
./run_service.sh
```

## Health check

```bash
curl http://0.0.0.0:8808/healthz
```

## Scan a local source tree

`target_path` must be absolute because Docker bind mounts it into the TIVER container.

```bash
curl -s -X POST http://0.0.0.0:8808/scan \
  -H 'Content-Type: application/json' \
  -d '{
    "target_path": "/data/repos/redis",
    "job_name": "redis",
    "timeout_seconds": 7200
  }'
```

The response contains a `job_id`.

## Scan a Git repository

```bash
curl -s -X POST http://0.0.0.0:8808/scan \
  -H 'Content-Type: application/json' \
  -d '{
    "git_url": "https://github.com/redis/redis.git",
    "git_ref": "7.2.4",
    "job_name": "redis-7.2.4",
    "timeout_seconds": 7200
  }'
```

For paper-style reproduction, pick the repository revision deliberately. TIVER's README says examples use repositories around April 2022.

## Query job status

```bash
curl http://0.0.0.0:8808/jobs/<job_id>
```

Statuses:

```text
queued
running
succeeded
failed
```

## Fetch parsed result

```bash
curl http://0.0.0.0:8808/jobs/<job_id>/result
```

The parsed result includes:

```text
component_count
components[component].file_count
components[component].prevalent_version_by_file_count
components[component].version_counts_by_file
components[component].files
raw.onevpf
raw.epv
raw.verPerHash
```

The parsed result is derived from:

```text
existPaths_v/*_onevpf.txt
existPaths_v/*_epv.txt
verPerHash/*_vph.txt
```

## List and download artifacts

```bash
curl http://0.0.0.0:8808/jobs/<job_id>/artifacts
```

Download one artifact:

```bash
curl -O http://0.0.0.0:8808/jobs/<job_id>/artifacts/existPaths_v/redis_onevpf.txt
```

## One-shot CLI wrapper

```bash
./run_tiver_job.sh /absolute/path/to/source redis 7200
```

The CLI writes a new job under `./jobs` and prints the parsed JSON result.

## Layout

```text
tiver_service/
  app.py
  runner.py
  requirements.txt
  run_service.sh
  run_tiver_job.sh
  jobs/
    <job_id>/
      clonehere/        # only populated for git_url jobs
      res/
      funcs/
      output/
      existPaths/
      existPaths_v/
      verPerHash/
      command.json
      meta.json
      request.json
      result.json
      status.json
      stdout.log
      stderr.log
```

## Notes

TIVER's original scripts scan every directory under `/tiver/clonehere` and use fixed output directories. This wrapper isolates each scan in a separate job directory and mounts only one target repository into `/tiver/clonehere/<safe_repo_name>`.

The wrapper converts unsafe characters in `job_name` to hyphens. This also avoids underscores because the original `tiver.py` derives output names by splitting on underscores.
