# One-shot CVE scan workflow

This client/server workflow keeps BinaryNinja on the client and keeps GPU inference on the server.

## Required data layout

Place the downloaded CEBin vulnerability data here:

```text
CEBin/
  models/
    CEBin-Embedding-Cisco.bin
    CEBin-Comparison-Cisco.bin
  cebin-tokenizer/
  data/
    cve/
      cve-dataset.tar.zst          # optional raw archive
      cve-dataset/                 # extracted dataset directory, created from the archive
      cve-function-list.csv
      vuln_cache.json              # optional, not provided by CEBin by default
    indexes/
      cve/                         # generated automatically on first scan
```

Only `cve-dataset.tar.zst` and `cve-function-list.csv` are required for the CVE reference workflow. `BinaryCorp`, `Cisco`, and `Trex` are not required for the one-shot CVE scan.

If `data/cve/cve-dataset/` is missing but `data/cve/cve-dataset.tar.zst` exists, `client/scan_once.py` extracts it with system `tar` and `zstd`.

## Start server

The local `start_server.sh` is preserved. From `CEBin/client_server`:

```bash
./start_server.sh
```

Defaults are `PORT=9088`, `DEVICE=mps`, and `python3.11`.

## One command scan

From `CEBin/client_server`:

```bash
python3.11 client/scan_once.py --input /path/to/target_binary
```

Default server URL is `http://127.0.0.1:9088`.

On the first run, the script builds `CEBin/data/indexes/cve/` from the CVE dataset. Later runs reuse the index.

For a small test:

```bash
python3.11 client/scan_once.py \
  --input /path/to/target_binary \
  --max-reference-functions 1000 \
  --max-target-functions 10 \
  --top-k 10 \
  --rerank-top-k 3
```

## Output meaning

The script prints JSONL events to stdout.

`scan_result.matches[].reference` tells you the matched reference function, including package, CVE, function name, binary path, architecture, compiler, and optimization when present in the CEBin CVE dataset.

`scan_result.matches[].vulnerability` tells you whether this reference function is explicitly marked as a vulnerable function by `cve-function-list.csv`.

CEBin does not provide full CVE descriptions or severity in the model. If you want summaries/severity/references, provide an optional `data/cve/vuln_cache.json` keyed by CVE ID.
