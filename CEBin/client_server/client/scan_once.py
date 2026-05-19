#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
import requests

try:
    import faiss  # type: ignore
except ImportError as exc:
    raise SystemExit("faiss-cpu is required on the client side: python3.11 -m pip install faiss-cpu") from exc

SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT_SERVER_DIR = SCRIPT_DIR.parent
CEBIN_ROOT = CLIENT_SERVER_DIR.parent

# Make the script runnable from any current working directory.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from extract_binaryninja import extract_binary
except Exception as exc:
    raise SystemExit(
        "BinaryNinja extraction support is required on the client machine. "
        "Run this script on the machine where `import binaryninja` works."
    ) from exc


JSON = Dict[str, Any]


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def print_event(kind: str, **fields: Any) -> None:
    record = {"kind": kind}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def default_data_root() -> Path:
    return CEBIN_ROOT / "data"


def load_vuln_cache(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"vulnerability cache must be a JSON object: {path}")
    return data


def resolve_paths(args: argparse.Namespace) -> Dict[str, Path]:
    data_root = Path(args.data_root).expanduser().resolve()
    cve_root = Path(args.cve_root).expanduser().resolve() if args.cve_root else data_root / "cve"
    cve_dataset = Path(args.cve_dataset).expanduser().resolve() if args.cve_dataset else cve_root / "cve-dataset"
    cve_archive = Path(args.cve_archive).expanduser().resolve() if args.cve_archive else cve_root / "cve-dataset.tar.zst"
    cve_functions = Path(args.cve_functions).expanduser().resolve() if args.cve_functions else cve_root / "cve-function-list.csv"
    vuln_cache = Path(args.vuln_cache).expanduser().resolve() if args.vuln_cache else cve_root / "vuln_cache.json"
    index_dir = Path(args.index_dir).expanduser().resolve() if args.index_dir else data_root / "indexes" / "cve"
    return {
        "data_root": data_root,
        "cve_root": cve_root,
        "cve_dataset": cve_dataset,
        "cve_archive": cve_archive,
        "cve_functions": cve_functions,
        "vuln_cache": vuln_cache,
        "index_dir": index_dir,
    }


def maybe_extract_cve_archive(cve_dataset: Path, cve_archive: Path, cve_root: Path) -> None:
    if cve_dataset.is_dir():
        return
    if not cve_archive.is_file():
        raise FileNotFoundError(
            "CVE dataset directory is missing and archive was not found. Expected one of:\n"
            f"  extracted directory: {cve_dataset}\n"
            f"  archive: {cve_archive}\n"
            "Download cve-dataset.tar.zst and cve-function-list.csv from the CEBin data release."
        )
    if shutil.which("zstd") is None:
        raise RuntimeError(
            "cve-dataset.tar.zst exists but zstd is not installed. Install it first, for example: brew install zstd"
        )
    ensure_dir(cve_root)
    print_event("extract_cve_dataset", archive=str(cve_archive), output_root=str(cve_root))
    subprocess.run(
        ["tar", "--use-compress-program=zstd", "-xf", str(cve_archive), "-C", str(cve_root)],
        check=True,
    )
    if not cve_dataset.is_dir():
        candidates = [path for path in cve_root.iterdir() if path.is_dir() and path.name.lower().replace("_", "-") == "cve-dataset"]
        if candidates and candidates[0] != cve_dataset:
            candidates[0].rename(cve_dataset)
    if not cve_dataset.is_dir():
        raise RuntimeError(f"Archive extracted, but expected CVE dataset directory was not created: {cve_dataset}")


def load_cve_function_map(path: Path) -> Dict[str, List[str]]:
    if not path.is_file():
        raise FileNotFoundError(
            f"CVE function list not found: {path}\n"
            "Place cve-function-list.csv under CEBin/data/cve/ or pass --cve-functions."
        )
    mapping: Dict[str, List[str]] = {}
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.reader(fp)
        first = True
        for row in reader:
            if not row or len(row) < 2:
                continue
            cve = row[0].strip()
            func = row[1].strip()
            if first:
                first = False
                if cve.lower() in {"cve", "cve_id", "id"} and func.lower() in {"function", "func", "func_name"}:
                    continue
            if not cve or not func:
                continue
            mapping.setdefault(cve, [])
            if func not in mapping[cve]:
                mapping[cve].append(func)
    if not mapping:
        raise ValueError(f"No CVE/function rows were parsed from {path}")
    return mapping


def iter_cve_tsv_files(cve_dataset: Path, selected_cves: Optional[set[str]]) -> Iterator[Path]:
    files = sorted(cve_dataset.rglob("*.tsv"))
    for path in files:
        if selected_cves is not None and path.stem not in selected_cves:
            continue
        yield path


def parse_function_blob(value: str, source: str) -> Dict[str, List[str]]:
    text = value.strip()
    if not text:
        raise ValueError(f"empty function blob in {source}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        import ast
        data = ast.literal_eval(text)
    if not isinstance(data, dict):
        raise ValueError(f"function blob is not an object in {source}")
    parsed: Dict[str, List[str]] = {}
    for key, tokens in data.items():
        if not isinstance(tokens, list):
            raise ValueError(f"function token value is not a list in {source}")
        parsed[str(key)] = [str(token) for token in tokens]
    return parsed


def iter_reference_records(
    cve_dataset: Path,
    cve_function_map: Dict[str, List[str]],
    selected_cves: Optional[set[str]],
    max_reference_functions: Optional[int],
) -> Iterator[JSON]:
    produced = 0
    for tsv_path in iter_cve_tsv_files(cve_dataset, selected_cves):
        with tsv_path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp, delimiter="\t")
            if not reader.fieldnames:
                continue
            fieldnames = set(reader.fieldnames)
            function_col = "function" if "function" in fieldnames else "func_str" if "func_str" in fieldnames else None
            if function_col is None:
                raise ValueError(f"Missing function/func_str column in {tsv_path}")
            for row_no, row in enumerate(reader, 2):
                cve = (row.get("cve") or tsv_path.stem).strip()
                func_name = (row.get("func_name") or row.get("function_name") or "").strip()
                vuln_funcs = cve_function_map.get(cve, [])
                try:
                    function = parse_function_blob(row.get(function_col, ""), f"{tsv_path}:{row_no}")
                except Exception as exc:
                    eprint(f"[WARN] skip bad function blob at {tsv_path}:{row_no}: {exc}")
                    continue
                file_path = (row.get("file_path") or row.get("path") or "").strip()
                binary_name = os.path.basename(file_path) if file_path else "unknown"
                meta = {
                    "source": "cebin-cve-dataset",
                    "cve": cve,
                    "package": (row.get("package") or "unknown").strip() or "unknown",
                    "file_path": file_path,
                    "binary": binary_name,
                    "func_name": func_name,
                    "func_addr": (row.get("address") or row.get("addr") or "").strip(),
                    "arch": (row.get("arch") or "unknown").strip() or "unknown",
                    "compiler": (row.get("compiler") or "unknown").strip() or "unknown",
                    "optimizer": (row.get("opt") or row.get("optimizer") or "unknown").strip() or "unknown",
                    "bb_cnt": safe_int(row.get("bb_cnt")),
                    "instr_cnt": safe_int(row.get("instr_cnt")),
                    "is_marked_vulnerable_function": bool(func_name and func_name in vuln_funcs),
                    "vulnerable_functions_for_cve": vuln_funcs,
                    "dataset_file": str(tsv_path),
                }
                yield {"meta": meta, "function": function}
                produced += 1
                if max_reference_functions is not None and produced >= max_reference_functions:
                    return


def safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def chunks(items: Iterable[JSON], batch_size: int) -> Iterator[List[JSON]]:
    batch: List[JSON] = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def post_health(server: str) -> JSON:
    response = requests.get(server.rstrip("/") + "/v1/health", timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"health failed: HTTP {response.status_code}: {response.text}")
    data = response.json()
    print_event("health", server=server, response=data)
    return data


def post_embed(server: str, records: Sequence[JSON], encoder: str, max_length: int) -> np.ndarray:
    payload = {
        "functions": [record["function"] for record in records],
        "encoder": encoder,
        "max_length": max_length,
        "pad_to_multiple_of": 8,
    }
    response = requests.post(server.rstrip("/") + "/v1/embed", json=payload, timeout=None)
    if response.status_code != 200:
        raise RuntimeError(f"embed failed: HTTP {response.status_code}: {response.text}")
    embeddings = response.json().get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(records):
        raise RuntimeError("server returned an invalid embedding batch")
    return np.asarray(embeddings, dtype=np.float32)


def post_compare(server: str, pairs: Sequence[JSON], max_length: int) -> List[float]:
    payload = {
        "pairs": [{"left": pair["left"], "right": pair["right"]} for pair in pairs],
        "max_length": max_length,
        "pad_to_multiple_of": 8,
    }
    response = requests.post(server.rstrip("/") + "/v1/compare", json=payload, timeout=None)
    if response.status_code != 200:
        raise RuntimeError(f"compare failed: HTTP {response.status_code}: {response.text}")
    scores = response.json().get("scores")
    if not isinstance(scores, list) or len(scores) != len(pairs):
        raise RuntimeError("server returned an invalid comparison batch")
    return [float(score) for score in scores]


def index_files(index_dir: Path) -> Dict[str, Path]:
    return {
        "faiss": index_dir / "reference.faiss",
        "meta": index_dir / "reference_meta.jsonl",
        "meta_offsets": index_dir / "reference_meta.offsets.npy",
        "functions": index_dir / "reference_functions.jsonl",
        "function_offsets": index_dir / "reference_functions.offsets.npy",
        "config": index_dir / "index_config.json",
    }


def index_exists(index_dir: Path) -> bool:
    files = index_files(index_dir)
    return all(files[key].exists() for key in ("faiss", "meta", "meta_offsets", "functions", "function_offsets", "config"))


def write_jsonl_record(fp: Any, record: JSON, offsets: List[int]) -> None:
    offsets.append(fp.tell())
    fp.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_reference_index(args: argparse.Namespace, paths: Dict[str, Path]) -> None:
    cve_dataset = paths["cve_dataset"]
    cve_archive = paths["cve_archive"]
    cve_root = paths["cve_root"]
    cve_functions = paths["cve_functions"]
    index_dir = paths["index_dir"]

    maybe_extract_cve_archive(cve_dataset, cve_archive, cve_root)
    cve_function_map = load_cve_function_map(cve_functions)
    selected_cves = set(args.cve) if args.cve else None

    if index_dir.exists() and args.rebuild_index:
        shutil.rmtree(index_dir)
    ensure_dir(index_dir)
    files = index_files(index_dir)

    print_event(
        "build_reference_index_start",
        cve_dataset=str(cve_dataset),
        cve_functions=str(cve_functions),
        index_dir=str(index_dir),
        selected_cves=sorted(selected_cves) if selected_cves else None,
    )

    first_dim: Optional[int] = None
    index: Optional[Any] = None
    total = 0
    meta_offsets: List[int] = []
    function_offsets: List[int] = []

    tmp_dir = Path(tempfile.mkdtemp(prefix="cebin-index-build-", dir=str(index_dir)))
    try:
        tmp_meta = tmp_dir / "reference_meta.jsonl"
        tmp_functions = tmp_dir / "reference_functions.jsonl"
        with tmp_meta.open("w", encoding="utf-8") as meta_fp, tmp_functions.open("w", encoding="utf-8") as function_fp:
            records = iter_reference_records(
                cve_dataset=cve_dataset,
                cve_function_map=cve_function_map,
                selected_cves=selected_cves,
                max_reference_functions=args.max_reference_functions,
            )
            for batch in chunks(records, args.batch_size):
                embeddings = post_embed(args.server, batch, "key", args.max_length)
                if first_dim is None:
                    first_dim = int(embeddings.shape[1])
                    index = faiss.IndexFlatIP(first_dim)
                if embeddings.shape[1] != first_dim:
                    raise RuntimeError(f"embedding dimension changed: {embeddings.shape[1]} != {first_dim}")
                index.add(np.ascontiguousarray(embeddings))
                for record in batch:
                    write_jsonl_record(meta_fp, record["meta"], meta_offsets)
                    write_jsonl_record(function_fp, record, function_offsets)
                total += len(batch)
                print_event("build_reference_progress", functions=total)

        if index is None or total == 0:
            raise RuntimeError("No reference functions were indexed.")

        faiss.write_index(index, str(files["faiss"]))
        shutil.move(str(tmp_meta), str(files["meta"]))
        shutil.move(str(tmp_functions), str(files["functions"]))
        np.save(files["meta_offsets"], np.asarray(meta_offsets, dtype=np.int64))
        np.save(files["function_offsets"], np.asarray(function_offsets, dtype=np.int64))
        config = {
            "created_at": int(time.time()),
            "source": "cebin-cve-dataset",
            "cve_dataset": str(cve_dataset),
            "cve_functions": str(cve_functions),
            "functions": total,
            "embedding_dim": first_dim,
            "faiss_metric": "inner_product",
            "selected_cves": sorted(selected_cves) if selected_cves else None,
        }
        files["config"].write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print_event("build_reference_index_done", index_dir=str(index_dir), functions=total, embedding_dim=first_dim)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class ReferenceIndex:
    def __init__(self, index_dir: Path) -> None:
        files = index_files(index_dir)
        self.index_dir = index_dir
        self.index = faiss.read_index(str(files["faiss"]))
        self.meta_path = files["meta"]
        self.function_path = files["functions"]
        self.meta_offsets = np.load(files["meta_offsets"])
        self.function_offsets = np.load(files["function_offsets"])
        if self.index.ntotal != len(self.meta_offsets) or self.index.ntotal != len(self.function_offsets):
            raise RuntimeError("Reference index and metadata offsets have different lengths.")

    def _read_at(self, path: Path, offsets: np.ndarray, idx: int) -> JSON:
        if idx < 0 or idx >= len(offsets):
            raise IndexError(idx)
        with path.open("r", encoding="utf-8") as fp:
            fp.seek(int(offsets[idx]))
            line = fp.readline()
        return json.loads(line)

    def meta(self, idx: int) -> JSON:
        return self._read_at(self.meta_path, self.meta_offsets, idx)

    def function_record(self, idx: int) -> JSON:
        return self._read_at(self.function_path, self.function_offsets, idx)


def extract_target_records(args: argparse.Namespace) -> Iterator[JSON]:
    return extract_binary(
        binary_path=str(Path(args.input).expanduser().resolve()),
        package=args.package,
        arch=args.arch,
        compiler=args.compiler,
        optimizer=args.optimizer,
        binary_name=args.binary_name,
        worker_threads=args.worker_threads,
        max_functions=args.max_target_functions,
    )


def vulnerability_payload(meta: JSON, vuln_cache: Dict[str, Any]) -> JSON:
    cve = str(meta.get("cve") or "")
    details = vuln_cache.get(cve) if cve else None
    return {
        "cve": cve,
        "is_marked_vulnerable_function": bool(meta.get("is_marked_vulnerable_function")),
        "vulnerable_functions_for_cve": meta.get("vulnerable_functions_for_cve") or [],
        "details": details,
    }


def scan_binary(args: argparse.Namespace, paths: Dict[str, Path]) -> None:
    ref = ReferenceIndex(paths["index_dir"])
    vuln_cache = load_vuln_cache(paths["vuln_cache"] if paths["vuln_cache"].exists() else None)
    print_event("scan_start", input=str(Path(args.input).expanduser().resolve()), index_dir=str(paths["index_dir"]), indexed_functions=int(ref.index.ntotal))

    total_targets = 0
    for batch in chunks(extract_target_records(args), args.batch_size):
        target_embeddings = post_embed(args.server, batch, "query", args.max_length)
        scores, indices = ref.index.search(np.ascontiguousarray(target_embeddings), args.top_k)
        for row, target_record in enumerate(batch):
            total_targets += 1
            candidates: List[JSON] = []
            compare_pairs: List[JSON] = []
            compare_candidate_positions: List[int] = []
            for rank, (score, idx) in enumerate(zip(scores[row].tolist(), indices[row].tolist()), 1):
                if idx < 0:
                    continue
                ref_meta = ref.meta(int(idx))
                if args.only_vulnerable and not bool(ref_meta.get("is_marked_vulnerable_function")):
                    continue
                candidate = {
                    "rank": rank,
                    "reference_index": int(idx),
                    "retrieval_score": float(score),
                    "comparison_score": None,
                    "reference": ref_meta,
                    "vulnerability": vulnerability_payload(ref_meta, vuln_cache),
                }
                candidates.append(candidate)
                if len(compare_pairs) < args.rerank_top_k:
                    ref_record = ref.function_record(int(idx))
                    compare_pairs.append({"left": target_record["function"], "right": ref_record["function"]})
                    compare_candidate_positions.append(len(candidates) - 1)
            if compare_pairs:
                compare_scores = post_compare(args.server, compare_pairs, args.max_length)
                for pos, score in zip(compare_candidate_positions, compare_scores):
                    candidates[pos]["comparison_score"] = score
                candidates.sort(key=lambda item: (
                    item["comparison_score"] if item["comparison_score"] is not None else float("-inf"),
                    item["retrieval_score"],
                ), reverse=True)
            result = {
                "target": target_record.get("meta", {}),
                "matches": candidates[: args.output_top_k],
            }
            print_event("scan_result", **result)
    print_event("scan_done", target_functions=total_targets)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot CEBin CVE scan: build/reuse reference index, extract target with BinaryNinja, query server, print matches.",
    )
    parser.add_argument("--input", required=True, help="Target binary path. This is the raw executable/dylib/so path.")
    parser.add_argument("--server", default="http://127.0.0.1:9088")

    parser.add_argument("--data-root", default=str(default_data_root()), help="Default: <CEBin>/data")
    parser.add_argument("--cve-root", help="Default: <data-root>/cve")
    parser.add_argument("--cve-dataset", help="Extracted CVE dataset directory. Default: <cve-root>/cve-dataset")
    parser.add_argument("--cve-archive", help="Optional cve-dataset.tar.zst. Default: <cve-root>/cve-dataset.tar.zst")
    parser.add_argument("--cve-functions", help="cve-function-list.csv. Default: <cve-root>/cve-function-list.csv")
    parser.add_argument("--vuln-cache", help="Optional CVE detail cache JSON. Default: <cve-root>/vuln_cache.json")
    parser.add_argument("--index-dir", help="Default: <data-root>/indexes/cve")
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--cve", action="append", help="Limit index build to a CVE. Can be passed multiple times.")

    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--rerank-top-k", type=int, default=5)
    parser.add_argument("--output-top-k", type=int, default=5)
    parser.add_argument("--only-vulnerable", action="store_true", help="Only print candidates marked as vulnerable functions in cve-function-list.csv.")

    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--max-reference-functions", type=int, help="Debug/testing only. Stops index build after N reference functions.")
    parser.add_argument("--max-target-functions", type=int, help="Debug/testing only. Stops target extraction after N functions.")

    parser.add_argument("--package", default="auto")
    parser.add_argument("--arch", default="auto")
    parser.add_argument("--compiler", default="unknown")
    parser.add_argument("--optimizer", default="unknown")
    parser.add_argument("--binary-name")
    parser.add_argument("--worker-threads", type=int, default=2)
    args = parser.parse_args()
    if args.top_k <= 0 or args.rerank_top_k < 0 or args.output_top_k <= 0:
        parser.error("--top-k and --output-top-k must be positive, --rerank-top-k must be non-negative")
    if args.rerank_top_k > args.top_k:
        parser.error("--rerank-top-k must be <= --top-k")
    return args


def main() -> None:
    args = parse_args()
    paths = resolve_paths(args)
    post_health(args.server)
    if args.rebuild_index or not index_exists(paths["index_dir"]):
        build_reference_index(args, paths)
    else:
        print_event("reference_index_reuse", index_dir=str(paths["index_dir"]))
    scan_binary(args, paths)


if __name__ == "__main__":
    main()
