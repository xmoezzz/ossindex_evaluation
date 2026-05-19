# CEBin client/server split

This split keeps BinaryNinja on the client machine and keeps the server limited to GPU inference.

## Layout

- `client/extract_binaryninja.py`: local BinaryNinja MLIL extraction and CEBin tokenization.
- `client/embed_remote.py`: sends tokenized functions to the GPU server and writes embeddings.
- `client/build_faiss_index.py`: builds a local FAISS index from reference embeddings.
- `client/query_faiss.py`: queries a local reference FAISS index with target embeddings.
- `client/make_pairs_from_search.py`: converts FAISS candidates into pairwise comparison requests.
- `client/compare_remote.py`: sends already-tokenized function pairs to the GPU server for pairwise scoring.
- `server/app.py`: FastAPI inference server.
- `server/cebin_inference.py`: model loading, padding, embedding inference, and comparison inference.

The server does not import BinaryNinja and does not process raw binaries.

## Server

Install server dependencies on the GPU machine:

```bash
cd /path/to/CEBin/client_server
python3 -m venv .venv-server
source .venv-server/bin/activate
pip install -r requirements-server.txt
```

Run the server:

```bash
python server/app.py \
  --cebin-root /path/to/CEBin \
  --embedding-model /path/to/CEBin/models/CEBin-Embedding-Cisco.bin \
  --comparison-model /path/to/CEBin/models/CEBin-Comparison-Cisco.bin \
  --device cuda:0 \
  --host 0.0.0.0 \
  --port 8000
```

For embedding-only deployment, omit `--comparison-model`.

## Client: extract and tokenize with BinaryNinja

Run this on the licensed BinaryNinja machine:

```bash
cd /path/to/CEBin/client_server
python3 -m venv .venv-client
source .venv-client/bin/activate
pip install -r requirements-client.txt
```

The BinaryNinja Python module must be available in this client environment.

```bash
python client/extract_binaryninja.py \
  --cebin-root /path/to/CEBin \
  --tokenizer /path/to/CEBin/cebin-tokenizer \
  --binary /path/to/target.bin \
  --package target-package \
  --arch x64 \
  --compiler unknown \
  --optimizer unknown \
  --output target.functions.jsonl
```

## Client: remote embedding inference

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

`compare_remote.py` expects JSONL records in this shape:

```json
{"left":{"input_ids":[1],"attention_mask":[1],"token_type_ids":[1]},"right":{"input_ids":[1],"attention_mask":[1],"token_type_ids":[1]},"cebin":{"pad_token_id":1}}
```

Run:

```bash
python client/compare_remote.py \
  --pairs target.pairs.jsonl \
  --server http://GPU_SERVER:8000 \
  --output pair_scores.jsonl
```
