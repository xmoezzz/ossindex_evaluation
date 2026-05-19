#!/usr/bin/env python3
import argparse
import json
import os
from typing import Iterable, Iterator, List

import numpy as np
import requests


def iter_records(path: str) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}") from exc


def chunks(iterator: Iterable[dict], batch_size: int) -> Iterator[List[dict]]:
    batch: List[dict] = []
    for item in iterator:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def post_embed(server_url: str, batch: List[dict], encoder: str, max_length: int) -> List[List[float]]:
    payload = {
        "functions": [record["function"] for record in batch],
        "encoder": encoder,
        "max_length": max_length,
        "pad_to_multiple_of": 8,
    }
    response = requests.post(server_url.rstrip("/") + "/v1/embed", json=payload, timeout=None)
    if response.status_code != 200:
        raise RuntimeError(f"server returned {response.status_code}: {response.text}")
    data = response.json()
    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(batch):
        raise RuntimeError("server returned an invalid embedding batch")
    return embeddings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send raw BinaryNinja MLIL functions to a CEBin inference server.")
    parser.add_argument("--input", required=True, help="Raw function JSONL from extract_binaryninja.py.")
    parser.add_argument("--server", required=True, help="Server URL, for example http://127.0.0.1:8000.")
    parser.add_argument("--output", required=True, help="Output .npz path for embeddings.")
    parser.add_argument("--meta-output", required=True, help="Output JSONL path for metadata.")
    parser.add_argument("--encoder", choices=["query", "key"], default="query")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=1024)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    embeddings: List[List[float]] = []
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.meta_output)) or ".", exist_ok=True)
    with open(args.meta_output, "w", encoding="utf-8") as meta_fp:
        for batch in chunks(iter_records(args.input), args.batch_size):
            batch_embeddings = post_embed(args.server, batch, args.encoder, args.max_length)
            embeddings.extend(batch_embeddings)
            for record in batch:
                meta_fp.write(json.dumps(record.get("meta", {}), separators=(",", ":")) + "\n")
            print(f"processed {len(embeddings)} functions")
    if not embeddings:
        raise SystemExit("No embeddings were produced.")
    arr = np.asarray(embeddings, dtype=np.float32)
    np.savez_compressed(args.output, embeddings=arr)
    print(f"wrote {arr.shape[0]} embeddings with dimension {arr.shape[1]} to {args.output}")


if __name__ == "__main__":
    main()
