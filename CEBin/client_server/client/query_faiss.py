#!/usr/bin/env python3
import argparse
import json
from typing import List

import faiss
import numpy as np


def load_meta(path: str) -> List[dict]:
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query a local FAISS index with CEBin embeddings.")
    parser.add_argument("--query-embeddings", required=True)
    parser.add_argument("--query-meta", required=True)
    parser.add_argument("--reference-index", required=True)
    parser.add_argument("--reference-meta", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--top-k", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    index = faiss.read_index(args.reference_index)
    query_vectors = np.asarray(np.load(args.query_embeddings)["embeddings"], dtype=np.float32)
    query_meta = load_meta(args.query_meta)
    reference_meta = load_meta(args.reference_meta)
    if len(query_meta) != query_vectors.shape[0]:
        raise SystemExit("query metadata count does not match query embeddings")
    if len(reference_meta) != index.ntotal:
        raise SystemExit("reference metadata count does not match reference index")
    scores, ids = index.search(query_vectors, args.top_k)
    with open(args.output, "w", encoding="utf-8") as out:
        for q_idx, q_meta in enumerate(query_meta):
            matches = []
            for score, ref_id in zip(scores[q_idx], ids[q_idx]):
                if ref_id < 0:
                    continue
                matches.append({"score": float(score), "reference": reference_meta[int(ref_id)]})
            out.write(json.dumps({"query": q_meta, "matches": matches}, separators=(",", ":")) + "\n")
    print(f"wrote search results for {len(query_meta)} query functions to {args.output}")


if __name__ == "__main__":
    main()
