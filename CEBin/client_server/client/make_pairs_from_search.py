#!/usr/bin/env python3
import argparse
import json
from typing import Dict, Iterator, Tuple


def meta_key(meta: dict) -> str:
    return json.dumps(meta, sort_keys=True, separators=(",", ":"))


def iter_jsonl(path: str) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}") from exc


def load_functions(path: str) -> Dict[str, dict]:
    mapping: Dict[str, dict] = {}
    for record in iter_jsonl(path):
        key = meta_key(record.get("meta", {}))
        if key in mapping:
            raise ValueError(f"Duplicate function metadata in {path}: {key}")
        mapping[key] = record
    return mapping


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build comparison pairs from FAISS search results.")
    parser.add_argument("--search-results", required=True, help="JSONL from query_faiss.py.")
    parser.add_argument("--query-functions", required=True, help="Raw query JSONL from extract_binaryninja.py.")
    parser.add_argument("--reference-functions", required=True, help="Raw reference JSONL from extract_binaryninja.py.")
    parser.add_argument("--output", required=True, help="Output pair JSONL for compare_remote.py.")
    parser.add_argument("--top-k", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    query_functions = load_functions(args.query_functions)
    reference_functions = load_functions(args.reference_functions)
    written = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for row in iter_jsonl(args.search_results):
            query_meta = row["query"]
            query_record = query_functions[meta_key(query_meta)]
            matches = row.get("matches", [])[: args.top_k]
            for match in matches:
                reference_meta = match["reference"]
                reference_record = reference_functions[meta_key(reference_meta)]
                record = {
                    "left": query_record["function"],
                    "right": reference_record["function"],
                    "query_meta": query_meta,
                    "reference_meta": reference_meta,
                    "retrieval_score": match["score"],
                    "cebin": query_record.get("cebin") or reference_record.get("cebin") or {},
                }
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
                written += 1
    if written == 0:
        raise SystemExit("No pairs were written.")
    print(f"wrote {written} comparison pairs to {args.output}")


if __name__ == "__main__":
    main()
