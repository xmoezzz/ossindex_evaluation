#!/usr/bin/env python3
import argparse
import json
from typing import Iterator, List, Optional

import requests


def iter_pairs(path: str) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if "left" not in data or "right" not in data:
                raise ValueError(f"Missing left/right at {path}:{line_no}")
            yield data


def chunks(iterator: Iterator[dict], batch_size: int) -> Iterator[List[dict]]:
    batch: List[dict] = []
    for item in iterator:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def infer_pad_token_id(batch: List[dict], explicit: Optional[int]) -> int:
    if explicit is not None:
        return explicit
    for pair in batch:
        for side in ("left", "right"):
            cebin = pair.get(side + "_cebin") or pair.get("cebin") or {}
            if "pad_token_id" in cebin:
                return int(cebin["pad_token_id"])
    raise ValueError("pad_token_id is missing. Pass --pad-token-id.")


def post_compare(server_url: str, batch: List[dict], pad_token_id: int, max_length: int) -> List[float]:
    payload = {
        "pairs": [{"left": item["left"], "right": item["right"]} for item in batch],
        "pad_token_id": pad_token_id,
        "max_length": max_length,
        "pad_to_multiple_of": 8,
    }
    response = requests.post(server_url.rstrip("/") + "/v1/compare", json=payload, timeout=None)
    if response.status_code != 200:
        raise RuntimeError(f"server returned {response.status_code}: {response.text}")
    scores = response.json().get("scores")
    if not isinstance(scores, list) or len(scores) != len(batch):
        raise RuntimeError("server returned an invalid comparison batch")
    return [float(score) for score in scores]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score CEBin function pairs with the remote GPU server.")
    parser.add_argument("--pairs", required=True, help="JSONL containing left/right tokenized function features.")
    parser.add_argument("--server", required=True)
    parser.add_argument("--output", required=True, help="Output JSONL with scores appended.")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--pad-token-id", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    written = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for batch in chunks(iter_pairs(args.pairs), args.batch_size):
            pad_token_id = infer_pad_token_id(batch, args.pad_token_id)
            scores = post_compare(args.server, batch, pad_token_id, args.max_length)
            for item, score in zip(batch, scores):
                record = dict(item)
                record["score"] = score
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
                written += 1
            print(f"processed {written} pairs")
    if written == 0:
        raise SystemExit("No pairs were scored.")


if __name__ == "__main__":
    main()
