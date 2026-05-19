# Server-side CVE scan workflow

This version keeps the original deployment goal:

```text
client machine:
  BinaryNinja only
  extracts raw MLIL function records from the target binary
  sends those records to the server

server machine:
  tokenizer
  CEBin embedding model
  CEBin comparison model
  CVE reference dataset
  FAISS reference index
  retrieval and reranking
```

The client no longer needs the CVE dataset, FAISS index, tokenizer, or model files.

## Server data layout

Put the CEBin files on the server like this:

```text
CEBin/
  models/
    CEBin-Embedding-Cisco.bin
    CEBin-Comparison-Cisco.bin

  cebin-tokenizer/

  data/
    cve/
      cve-dataset.tar.zst
      cve-function-list.csv
      cve-dataset/                 # created automatically on first /v1/scan if missing

    indexes/
      cve/                         # created automatically on first /v1/scan if missing
```

The server does not need BinaryNinja.

## Start server

Your local wrapper is preserved:

```bash
cd CEBin/client_server
./start_server.sh
```

The wrapper defaults to:

```text
PORT=9088
DEVICE=mps
python3.11
```

## Check server state

```bash
curl http://127.0.0.1:9088/v1/health
curl http://127.0.0.1:9088/v1/scan/status
```

If `cve-dataset/` or the FAISS index does not exist, the first `/v1/scan` request prepares them on the server.

## Client scan command

Run this on the machine with BinaryNinja Python API:

```bash
cd CEBin/client_server
python3.11 client/scan_remote.py --input /path/to/target_binary
```

Default server URL:

```text
http://127.0.0.1:9088
```

Remote server example:

```bash
python3.11 client/scan_remote.py \
  --input /path/to/target_binary \
  --server http://SERVER_IP:9088
```

Small test run:

```bash
python3.11 client/scan_remote.py \
  --input /Applications/QQ.app/Contents/MacOS/QQ \
  --max-target-functions 10 \
  --max-reference-functions 1000 \
  --top-k 10 \
  --rerank-top-k 3
```

`--max-reference-functions` is only for debugging. If it is used while the index does not exist, the server builds a partial debug index. For a full scan, delete `CEBin/data/indexes/cve/` or pass `--rebuild-index` without that limit.

## Output meaning

Each result is one target function with matched reference candidates. The server can report:

```text
package/library: from the CVE reference dataset `package` field
version: from the dataset if present, otherwise `unknown`
CVE: from the dataset `cve` field
function: from the dataset `func_name` field
vulnerability scope:
  function             if cve-function-list.csv marks this function for that CVE
  package_or_pool      if it only comes from the CVE search pool
```

The model does not invent library versions or vulnerability details. If the downloaded CEBin CVE dataset lacks a version field, the output stays `unknown`.
