# Vulture remote service

This package wraps the public `ShangzhiXu/Vulture` codebase as an HTTP job service for remote client/server deployments.

## What it does

It exposes two VULTURE stages as an async service:

- TPL reuse detection via `TPLReuseDetector/Detector.py`
- 1-day vulnerability detection via `OneDayDetector/VersionBasedDetection.py`

The service does **not** rebuild TPLFILTER. It assumes the public VULTURE dataset is available under the service directory.

## Upstream references

- Code repository: `https://github.com/ShangzhiXu/Vulture`
- Public dataset: `https://zenodo.org/records/13824990`

## Expected service-side layout

Put the dataset files under `data/` like this:

```text
vulture_service/
  data/
    signature.zip
    aligned_patch_commits.zip   # only needed when run_oneday_detection=true
    Result.zip                  # optional, not used by the service
```

This version can auto-extract:

- `signature.zip` -> `data/signature/`
- `aligned_patch_commits.zip` -> `data/aligned_patch/` and `data/aligned_cpe/`

`Result.zip` is optional and is not used by the service.

## System requirements

Install these on the service host first:

```bash
sudo apt install clang-format universal-ctags git python3 python3-venv python3-pip
```

## Bootstrap

```bash
./bootstrap_vulture.sh
```

This will:

- clone `https://github.com/ShangzhiXu/Vulture.git` into `vendor/Vulture`
- create `.venv`
- install service requirements
- install the Vulture Python requirements

## Run

```bash
./run_service.sh
```

Default address:

```text
http://0.0.0.0:8808
```

## API

### Health

```bash
curl http://SERVER:8808/healthz
```

### Capabilities

```bash
curl http://SERVER:8808/capabilities
```

The service supports two input modes:

```text
upload  # remote mode, recommended
path    # local/shared-filesystem compatibility mode
```

For remote client/server deployment, use upload mode. Do not submit a client-local path such as `C:\...` or `/home/client/...`, because the service process cannot access the client filesystem.

### Submit an archive scan, remote mode

Create an archive from the prepared scan directory on the client side, then upload it:

```bash
cd /path/to/expanded_root
zip -r /tmp/sample_001.zip .

curl -s -X POST http://SERVER:8808/api/v1/analyze/upload \
  -F "file=@/tmp/sample_001.zip" \
  -F "job_name=sample_001" \
  -F "input_kind=archive" \
  -F "run_tpl_reuse=true" \
  -F "run_oneday_detection=true" \
  -F "timeout_seconds=21600" \
  -F "keep_workdir=false"
```

The server stores the uploaded archive under its own `work/<job_id>/upload/` directory, safely extracts it into the job work directory, and runs VULTURE against the extracted service-side path.

Supported upload archive formats:

```text
zip
tar / tar.gz / tgz / tar.bz2 / tbz / tbz2 / tar.xz / txz
```

### Submit a single-file scan, remote mode

```bash
curl -s -X POST http://SERVER:8808/api/v1/analyze/upload \
  -F "file=@/path/to/input.c" \
  -F "job_name=single_file" \
  -F "input_kind=file" \
  -F "run_tpl_reuse=true" \
  -F "run_oneday_detection=true"
```

### Submit a path scan, local/shared-filesystem compatibility mode

This mode is only valid when the submitted path exists on the service host, or when the client and service share a mounted filesystem.

```bash
curl -s -X POST http://SERVER:8808/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "target_path": "/absolute/service-side/path/to/c_or_cpp_project",
    "job_name": "sample_001",
    "input_kind": "directory",
    "run_tpl_reuse": true,
    "run_oneday_detection": true
  }'
```

### Check status

```bash
curl http://SERVER:8808/api/v1/jobs/JOB_ID
```

### Get parsed result

```bash
curl http://SERVER:8808/api/v1/jobs/JOB_ID/result
```

### List artifacts

```bash
curl http://SERVER:8808/api/v1/jobs/JOB_ID/artifacts
```

### Cancel

```bash
curl -X POST http://SERVER:8808/api/v1/jobs/JOB_ID/cancel
```

## Result fields

For upload mode, completed results include:

```text
input_mode: upload
target_path: null
uploaded_filename: original uploaded filename
uploaded_size: uploaded file size in bytes
input_kind: archive or file
```

For path mode, completed results include:

```text
input_mode: path
target_path: service-side target path
input_kind: directory or file
```

### Data

https://zenodo.org/records/13824990
