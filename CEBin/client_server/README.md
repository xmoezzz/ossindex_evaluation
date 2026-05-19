# CEBin client/server split

This split keeps BinaryNinja on the client machine and keeps the server responsible for CEBin tokenizer plus GPU inference. The client never needs CEBin model files and no longer needs the tokenizer.

## Layout

- `client/extract_binaryninja.py`: local BinaryNinja MLIL extraction only. It writes raw function JSONL.
- `client/embed_remote.py`: sends raw functions to the inference server and writes embeddings.
- `client/build_faiss_index.py`: builds a local FAISS index from reference embeddings.
- `client/query_faiss.py`: queries a local reference FAISS index with target embeddings.
- `client/make_pairs_from_search.py`: converts FAISS candidates into pairwise comparison requests.
- `client/compare_remote.py`: sends raw function pairs to the inference server for pairwise scoring.
- `server/app.py`: FastAPI inference server.
- `server/cebin_inference.py`: tokenizer loading, model loading, padding, embedding inference, and comparison inference.

The server does not import BinaryNinja and does not process raw binaries. The client does not load CEBin tokenizer or models.

## Server

Install server dependencies on the GPU machine:

```bash
cd /path/to/CEBin/client_server
python3 -m venv .venv-server
source .venv-server/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements-server.txt
```

Run the server with the wrapper:

```bash
cd /path/to/CEBin/client_server
DEVICE=cuda:0 PORT=8000 ./start_server.sh
```

On a Mac smoke-test machine, use:

```bash
DEVICE=mps PORT=9088 ./start_server.sh
```

The wrapper expects:

```text
CEBin/models/CEBin-Embedding-Cisco.bin
CEBin/models/CEBin-Comparison-Cisco.bin
CEBin/cebin-tokenizer/
```

Manual server command:

```bash
python server/app.py \
  --cebin-root /path/to/CEBin \
  --embedding-model /path/to/CEBin/models/CEBin-Embedding-Cisco.bin \
  --comparison-model /path/to/CEBin/models/CEBin-Comparison-Cisco.bin \
  --tokenizer /path/to/CEBin/cebin-tokenizer \
  --device cuda:0 \
  --host 0.0.0.0 \
  --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/v1/health
```

## Client: extract raw functions with BinaryNinja

Run this on the licensed BinaryNinja machine:

```bash
cd /path/to/CEBin/client_server
python3 -m venv .venv-client
source .venv-client/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements-client.txt
```

The BinaryNinja Python module must be available in this client environment.

Minimal extraction command:

```bash
python client/extract_binaryninja.py /path/to/target.bin \
  -o target.functions.jsonl
```

Optional metadata:

```bash
python client/extract_binaryninja.py /path/to/target.bin \
  --package target-package \
  --compiler clang \
  --optimizer O2 \
  -o target.functions.jsonl
```

## Client: remote embedding inference

For target/query functions:

```bash
python client/embed_remote.py \
  --input target.functions.jsonl \
  --server http://GPU_SERVER:8000 \
  --encoder query \
  --output target.embeddings.npz \
  --meta-output target.meta.jsonl
```

For a reference corpus, use `--encoder key`:

```bash
python client/embed_remote.py \
  --input reference.functions.jsonl \
  --server http://GPU_SERVER:8000 \
  --encoder key \
  --output reference.embeddings.npz \
  --meta-output reference.meta.jsonl
```

## Client: local FAISS search

```bash
python client/build_faiss_index.py \
  --embeddings reference.embeddings.npz \
  --index reference.faiss

python client/query_faiss.py \
  --query-embeddings target.embeddings.npz \
  --query-meta target.meta.jsonl \
  --reference-index reference.faiss \
  --reference-meta reference.meta.jsonl \
  --top-k 20 \
  --output target.search.jsonl
```

## Client: pairwise comparison reranking

Build candidate pairs from FAISS results:

```bash
python client/make_pairs_from_search.py \
  --search-results target.search.jsonl \
  --query-functions target.functions.jsonl \
  --reference-functions reference.functions.jsonl \
  --top-k 20 \
  --output target.pairs.jsonl
```

Run pairwise comparison:

```bash
python client/compare_remote.py \
  --pairs target.pairs.jsonl \
  --server http://GPU_SERVER:8000 \
  --output pair_scores.jsonl
```
## Server-side CVE scan

For the current deployment target, BinaryNinja stays on the client and all CVE dataset/index/search/rerank work stays on the server. See `SERVER_SCAN.md`.

Minimal client scan command:

```bash
python3.11 client/scan_remote.py --input /path/to/target_binary
```

Server-side data goes under:

```text
CEBin/data/cve/cve-dataset.tar.zst
CEBin/data/cve/cve-function-list.csv
CEBin/data/indexes/cve/
```

