# OSSDetector service conversion

This package keeps the original OSSDetector scripts and adds a service-oriented interface under `ossdetector_service/`.

The original detector is a batch script. The new implementation loads the OSSDetector database once, builds a reverse index from function hash to component, and exposes both HTTP and JSONL batch interfaces.

## Data layout

Prepare a data directory with this exact layout after initialization:

```text
/path/to/ossdetector-data/
  componentDB_ours_6.0/
  initialSigs_ours/
  metaInfos_ours_6.0/
    aveFuncs
    weights_ours_6.0/
  verIDX_ours/
  repo-date/
```

Default paths used by the converted CLI/server:

```text
./data/                      # Zenodo archives by default
./data/ossdetector/           # initialized data layout by default
```

The four Zenodo archives map to these directories:

```text
component.tar.gz  -> componentDB_ours_6.0/
initial.tar.gz    -> initialSigs_ours/
meta.tar.gz       -> metaInfos_ours_6.0/ and repo-date/ if included there
ver.tar.gz        -> verIDX_ours/
```

If the archive extracts with a different top-level directory name, create a symlink with the required name instead of moving data blindly.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-service.txt
```

## Initialize data from Zenodo archives

Put the four downloaded archives in `./data/` by default:

```text
./data/
  component.tar.gz
  initial.tar.gz
  meta.tar.gz
  ver.tar.gz
```

Then run:

```bash
python3 -m ossdetector_service.cli init-data
```

By default this reads archives from `./data/`, extracts into `./data/ossdetector/_extracted/`, and creates symlinks with the exact names required by the service. It writes `./data/ossdetector/.ossdetector_init.json`; if the layout is already complete, later `init-data` runs skip extraction automatically. Use `--force` to rebuild the layout. Use `--copy` if the target filesystem cannot use symlinks.

Validate the initialized index:

```bash
python3 -m ossdetector_service.cli stats
```

## Run as an HTTP service

```bash
python3 -m ossdetector_service.server \
  --host 0.0.0.0 \
  --port 8088
```

Check status:

```bash
curl http://127.0.0.1:8088/health
```

Detect from a hash list:

```bash
curl -s http://127.0.0.1:8088/detect/hashes \
  -H 'Content-Type: application/json' \
  -d '{
    "input_name": "my-target",
    "hashes": [
      {"hash": "abc", "path": "src/a.c"},
      {"hash": "def", "path": "src/b.c"}
    ]
  }'
```

Detect from an OSSDetector `.hidx` file:

```bash
curl -s http://127.0.0.1:8088/detect/hidx \
  -F input_name=my-target \
  -F file=@sample/mongodb@@mongo_fuzzy_.hidx
```

## Run as a batch CLI

One `.hidx` file:

```bash
python3 -m ossdetector_service.cli detect-hidx \
  --input sample/mongodb@@mongo_fuzzy_.hidx \
  --output-json output/mongodb.json
```

A directory of `.hidx` files:

```bash
python3 -m ossdetector_service.cli detect-hidx-dir \
  --input-dir sample \
  --output-jsonl output/results.jsonl
```

Generate `.hidx` from a C/C++ source tree:

```bash
python3 -m ossdetector_service.cli hash-source \
  --source-dir /path/to/source \
  --output output/source.hidx \
  --ctags-path /usr/local/bin/ctags
```

`hash-source` requires `py-tlsh` and a working ctags binary. The detector itself does not require TLSH when the input is already `.hidx` or hash JSON.

## API result shape

```json
{
  "input_name": "my-target",
  "matches": [
    {
      "component": "zlib",
      "component_sig": "zlib_sig",
      "predicted_version": "v1.2.11",
      "newest_version": "v1.3.1",
      "time_diff_seconds": 123456.0,
      "matched_function_count": 42,
      "total_function_count": 1000.0,
      "matched_ratio": 0.042,
      "matched_hashes": ["..."],
      "matched_paths": ["src/a.c"]
    }
  ],
  "warnings": []
}
```

## Compatibility notes

The matching thresholds are kept from the original `detector-TPL.py`:

```text
theta1 = 0.003
theta3 = 0.8
minimum common functions = 3
version score difference window = 0.1
```

The service intentionally removes the original current-working-directory dependency. All database paths are derived from `--data-dir`.
