#!/usr/bin/env python3
import argparse
import json
import os

import faiss
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local FAISS index from CEBin embeddings.")
    parser.add_argument("--embeddings", required=True, help=".npz file written by embed_remote.py.")
    parser.add_argument("--index", required=True, help="Output FAISS index path.")
    parser.add_argument("--metric", choices=["inner_product", "l2"], default="inner_product")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = np.load(args.embeddings)
    vectors = np.asarray(data["embeddings"], dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] == 0:
        raise SystemExit("embeddings must be a non-empty 2D array")
    dim = vectors.shape[1]
    if args.metric == "inner_product":
        index = faiss.IndexFlatIP(dim)
    else:
        index = faiss.IndexFlatL2(dim)
    index.add(vectors)
    os.makedirs(os.path.dirname(os.path.abspath(args.index)) or ".", exist_ok=True)
    faiss.write_index(index, args.index)
    print(f"wrote FAISS index with {index.ntotal} vectors and dimension {dim} to {args.index}")


if __name__ == "__main__":
    main()
