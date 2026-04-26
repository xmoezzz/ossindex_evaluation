# Vulture local service

This package wraps the public `ShangzhiXu/Vulture` codebase as a local HTTP job service.

## What it does

It exposes two VULTURE stages as a local async service:

- TPL reuse detection via `TPLReuseDetector/Detector.py`
- 1-day vulnerability detection via `OneDayDetector/VersionBasedDetection.py`

The service does **not** rebuild TPLFILTER. It assumes you already have the public VULTURE dataset extracted locally.

## Upstream references

- Code repository: `https://github.com/ShangzhiXu/Vulture`
- Public dataset: `https://zenodo.org/records/13824990`

## Expected local layout

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

Install these on the host first:

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
http://127.0.0.1:5681
```

## API

### Health

```bash
curl http://127.0.0.1:5681/healthz
```

### Capabilities

```bash
curl http://127.0.0.1:5681/capabilities
```

### Submit a directory scan

```bash
curl -s -X POST http://127.0.0.1:5681/scan \
  -H 'Content-Type: application/json' \
  -d '{
    "target_path": "/absolute/path/to/c_or_cpp_project",
    "job_name": "sample_001",
    "input_kind": "directory",
    "run_tpl_reuse": true,
    "run_oneday_detection": true
  }'
```

### Submit a single-file scan

```bash
curl -s -X POST http://127.0.0.1:5681/scan \
  -H 'Content-Type: application/json' \
  -d '{
    "target_path": "/absolute/path/to/input.c",
    "job_name": "single_file",
    "input_kind": "file",
    "run_tpl_reuse": true,
    "run_oneday_detection": true
  }'
```

### Check status

```bash
curl http://127.0.0.1:5681/jobs/JOB_ID
```

### Get parsed result

```bash
curl http://127.0.0.1:5681/jobs/JOB_ID/result
```

### List artifacts

```bash
curl http://127.0.0.1:5681/jobs/JOB_ID/artifacts
```

### Cancel

```bash
curl -X POST http://127.0.0.1:5681/jobs/JOB_ID/cancel
```

### Data
https://zenodo.org/records/13824990

