#!/usr/bin/env python3
from __future__ import annotations

import ast
import csv
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

try:
    import faiss  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("faiss-cpu is required on the server for /v1/scan") from exc

try:
    import zstandard as zstd  # type: ignore
except ImportError:  # pragma: no cover
    zstd = None

from cebin_inference import CEBinInference, FunctionInput


FUNCTION_COLUMNS = {"function", "func_str", "raw_function", "tokens", "tokenized_function"}
DEFAULT_INDEX_NAME = "reference.faiss"
DEFAULT_META_NAME = "reference_meta.jsonl"
DEFAULT_FUNCTION_NAME = "reference_functions.jsonl"
DEFAULT_OFFSET_NAME = "reference_function_offsets.npy"
DEFAULT_CONFIG_NAME = "reference_config.json"


@dataclass(frozen=True)
class ReferencePaths:
    cve_dir: Path
    dataset_archive: Path
    dataset_dir: Path
    cve_function_list: Path
    index_dir: Path
    index_file: Path
    meta_file: Path
    function_file: Path
    offset_file: Path
    config_file: Path

    @staticmethod
    def from_cebin_root(cebin_root: str) -> "ReferencePaths":
        root = Path(cebin_root).resolve()
        cve_dir = root / "data" / "cve"
        index_dir = root / "data" / "indexes" / "cve"
        return ReferencePaths(
            cve_dir=cve_dir,
            dataset_archive=cve_dir / "cve-dataset.tar.zst",
            dataset_dir=cve_dir / "cve-dataset",
            cve_function_list=cve_dir / "cve-function-list.csv",
            index_dir=index_dir,
            index_file=index_dir / DEFAULT_INDEX_NAME,
            meta_file=index_dir / DEFAULT_META_NAME,
            function_file=index_dir / DEFAULT_FUNCTION_NAME,
            offset_file=index_dir / DEFAULT_OFFSET_NAME,
            config_file=index_dir / DEFAULT_CONFIG_NAME,
        )


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _is_file_ready(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _find_dataset_files(dataset_dir: Path) -> List[Path]:
    if not dataset_dir.is_dir():
        return []
    files: List[Path] = []
    for suffix in ("*.tsv", "*.csv", "*.jsonl"):
        files.extend(dataset_dir.rglob(suffix))
    return sorted(path for path in files if path.is_file())


def _extract_tar_zst_with_python(archive: Path, output_dir: Path) -> None:
    if zstd is None:
        raise RuntimeError("zstandard Python package is not installed")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("rb") as compressed_fp:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(compressed_fp) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tf:
                tf.extractall(path=output_dir.parent)


def _extract_tar_zst_with_system_tar(archive: Path, output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    commands = [
        ["tar", "--use-compress-program=zstd", "-xf", str(archive), "-C", str(output_dir.parent)],
        ["tar", "-I", "zstd", "-xf", str(archive), "-C", str(output_dir.parent)],
        ["tar", "--zstd", "-xf", str(archive), "-C", str(output_dir.parent)],
    ]
    errors: List[str] = []
    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return
        except Exception as exc:
            errors.append(f"{' '.join(cmd)}: {exc}")
    raise RuntimeError("failed to extract cve-dataset.tar.zst with system tar: " + " | ".join(errors))


def ensure_cve_dataset(paths: ReferencePaths) -> None:
    if _find_dataset_files(paths.dataset_dir):
        return
    if not _is_file_ready(paths.dataset_archive):
        raise FileNotFoundError(
            f"missing CVE dataset archive: {paths.dataset_archive}. "
            "Put cve-dataset.tar.zst under CEBin/data/cve/."
        )
    tmp_parent = paths.cve_dir / ".extracting-cve-dataset"
    if tmp_parent.exists():
        shutil.rmtree(tmp_parent)
    tmp_parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp_dataset = tmp_parent / "cve-dataset"
        try:
            _extract_tar_zst_with_python(paths.dataset_archive, tmp_dataset)
        except Exception:
            shutil.rmtree(tmp_parent)
            tmp_parent.mkdir(parents=True, exist_ok=True)
            _extract_tar_zst_with_system_tar(paths.dataset_archive, tmp_dataset)

        extracted_files = _find_dataset_files(tmp_parent)
        if not extracted_files:
            raise RuntimeError(f"archive extracted no .tsv/.csv/.jsonl files: {paths.dataset_archive}")
        if paths.dataset_dir.exists():
            shutil.rmtree(paths.dataset_dir)
        # Prefer an extracted directory named cve-dataset, otherwise move the whole temporary tree.
        candidate = tmp_parent / "cve-dataset"
        if candidate.is_dir() and _find_dataset_files(candidate):
            shutil.move(str(candidate), str(paths.dataset_dir))
        else:
            paths.dataset_dir.mkdir(parents=True, exist_ok=True)
            for item in tmp_parent.iterdir():
                if item.name == "cve-dataset":
                    continue
                shutil.move(str(item), str(paths.dataset_dir / item.name))
    finally:
        if tmp_parent.exists():
            shutil.rmtree(tmp_parent)


def _read_csv_rows(path: Path) -> Iterator[Dict[str, str]]:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fp:
        sample = fp.read(4096)
        fp.seek(0)
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        if path.suffix.lower() not in {".tsv", ".csv"}:
            delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
        reader = csv.DictReader(fp, delimiter=delimiter)
        for row in reader:
            yield {str(k): "" if v is None else str(v) for k, v in row.items() if k is not None}


def _read_jsonl_rows(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line_no, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_no}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"JSONL row must be an object at {path}:{line_no}")
            yield obj


def _iter_table_rows(path: Path) -> Iterator[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        yield from _read_jsonl_rows(path)
    elif suffix in {".tsv", ".csv"}:
        yield from _read_csv_rows(path)


def _parse_function(value: Any) -> FunctionInput:
    if isinstance(value, dict):
        return value
    if value is None:
        raise ValueError("missing function field")
    text = str(value).strip()
    if not text:
        raise ValueError("empty function field")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = ast.literal_eval(text)
    if not isinstance(obj, dict):
        raise ValueError("parsed function field is not an object")
    return obj


def _get_function_value(row: Dict[str, Any]) -> Tuple[str, Any]:
    for key in FUNCTION_COLUMNS:
        if key in row:
            return key, row[key]
    for key in row:
        if key.lower() in FUNCTION_COLUMNS:
            return key, row[key]
    raise ValueError("row has no function column")


def _normalize_key(row: Dict[str, Any], *names: str) -> str:
    lower = {str(k).lower(): k for k in row.keys()}
    for name in names:
        key = lower.get(name.lower())
        if key is not None:
            value = row.get(key)
            return "" if value is None else str(value)
    return ""


def load_cve_function_map(path: Path) -> Dict[str, set[str]]:
    mapping: Dict[str, set[str]] = {}
    if not path.is_file():
        return mapping
    for row in _read_csv_rows(path):
        cve = _normalize_key(row, "cve", "cve_id", "id")
        func = _normalize_key(row, "func_name", "function", "func", "symbol")
        if not cve or not func:
            continue
        mapping.setdefault(cve, set()).add(func)
    return mapping


def iter_reference_records(paths: ReferencePaths) -> Iterator[Tuple[Dict[str, Any], FunctionInput]]:
    ensure_cve_dataset(paths)
    dataset_files = _find_dataset_files(paths.dataset_dir)
    if not dataset_files:
        raise RuntimeError(f"no dataset files found under {paths.dataset_dir}")
    vulnerable_functions = load_cve_function_map(paths.cve_function_list)
    for dataset_file in dataset_files:
        for row in _iter_table_rows(dataset_file):
            try:
                function_key, function_value = _get_function_value(row)
                function = _parse_function(function_value)
            except Exception:
                continue
            meta = {str(k): v for k, v in row.items() if str(k) != function_key}
            cve = _normalize_key(meta, "cve", "cve_id", "id")
            func_name = _normalize_key(meta, "func_name", "function_name", "symbol", "name")
            package = _normalize_key(meta, "package", "pkg", "library", "lib")
            version = _normalize_key(meta, "version", "package_version", "pkg_version")
            if not version:
                version = "unknown"
            meta.setdefault("cve", cve)
            meta.setdefault("func_name", func_name)
            meta.setdefault("package", package)
            meta.setdefault("version", version)
            meta.setdefault("dataset_file", str(dataset_file))
            marked = bool(cve and func_name and func_name in vulnerable_functions.get(cve, set()))
            meta["is_marked_vulnerable_function"] = marked
            meta["marked_vulnerable_functions_for_cve"] = sorted(vulnerable_functions.get(cve, set())) if cve else []
            yield meta, function


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class CveReferenceIndex:
    def __init__(self, paths: ReferencePaths, engine: CEBinInference, batch_size: int = 64) -> None:
        self.paths = paths
        self.engine = engine
        self.batch_size = batch_size
        self.index: Optional[Any] = None
        self.meta: List[Dict[str, Any]] = []
        self.offsets: Optional[np.ndarray] = None

    def status(self) -> Dict[str, Any]:
        return {
            "cve_dir": str(self.paths.cve_dir),
            "dataset_archive": str(self.paths.dataset_archive),
            "dataset_dir": str(self.paths.dataset_dir),
            "cve_function_list": str(self.paths.cve_function_list),
            "index_dir": str(self.paths.index_dir),
            "index_exists": self.paths.index_file.is_file(),
            "meta_exists": self.paths.meta_file.is_file(),
            "function_store_exists": self.paths.function_file.is_file(),
            "loaded": self.index is not None,
            "loaded_records": len(self.meta),
        }

    def ensure_loaded(self, rebuild: bool = False, max_reference_functions: Optional[int] = None) -> None:
        if rebuild or not self._index_files_ready():
            self.build(max_reference_functions=max_reference_functions)
        self.load()

    def _index_files_ready(self) -> bool:
        return all(
            _is_file_ready(path)
            for path in (self.paths.index_file, self.paths.meta_file, self.paths.function_file, self.paths.offset_file)
        )

    def load(self) -> None:
        if self.index is not None:
            return
        if not self._index_files_ready():
            raise RuntimeError("CVE reference index is not built")
        self.index = faiss.read_index(str(self.paths.index_file))
        self.meta = _load_jsonl(self.paths.meta_file)
        self.offsets = np.load(str(self.paths.offset_file))
        if int(self.index.ntotal) != len(self.meta):
            raise RuntimeError(f"index/meta size mismatch: {self.index.ntotal} vs {len(self.meta)}")
        if len(self.offsets) != len(self.meta):
            raise RuntimeError(f"offset/meta size mismatch: {len(self.offsets)} vs {len(self.meta)}")

    def build(self, max_reference_functions: Optional[int] = None) -> None:
        self.paths.index_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="cve-index-build-", dir=str(self.paths.index_dir)))
        tmp_index = tmp_dir / DEFAULT_INDEX_NAME
        tmp_meta = tmp_dir / DEFAULT_META_NAME
        tmp_functions = tmp_dir / DEFAULT_FUNCTION_NAME
        tmp_offsets = tmp_dir / DEFAULT_OFFSET_NAME
        tmp_config = tmp_dir / DEFAULT_CONFIG_NAME
        index = None
        offsets: List[int] = []
        total = 0
        batch_meta: List[Dict[str, Any]] = []
        batch_functions: List[FunctionInput] = []
        try:
            with tmp_meta.open("w", encoding="utf-8") as meta_fp, tmp_functions.open("w", encoding="utf-8") as func_fp:
                def flush() -> None:
                    nonlocal index, total, batch_meta, batch_functions
                    if not batch_functions:
                        return
                    embeddings = self.engine.embed(batch_functions, encoder="key")
                    arr = np.asarray(embeddings, dtype=np.float32)
                    if arr.ndim != 2 or arr.shape[0] != len(batch_functions):
                        raise RuntimeError("invalid reference embedding batch")
                    if index is None:
                        index = faiss.IndexFlatIP(arr.shape[1])
                    index.add(arr)
                    for meta, function in zip(batch_meta, batch_functions):
                        offsets.append(func_fp.tell())
                        func_fp.write(_json_dumps(function) + "\n")
                        meta_fp.write(_json_dumps(meta) + "\n")
                    total += len(batch_functions)
                    batch_meta = []
                    batch_functions = []
                    print(f"[INFO] built CVE reference embeddings: {total}", flush=True)

                for meta, function in iter_reference_records(self.paths):
                    batch_meta.append(meta)
                    batch_functions.append(function)
                    if len(batch_functions) >= self.batch_size:
                        flush()
                    if max_reference_functions is not None and total + len(batch_functions) >= max_reference_functions:
                        flush()
                        break
                flush()

            if index is None or total == 0:
                raise RuntimeError("no reference functions were indexed")
            faiss.write_index(index, str(tmp_index))
            np.save(str(tmp_offsets), np.asarray(offsets, dtype=np.int64))
            tmp_config.write_text(_json_dumps({"records": total, "metric": "inner_product"}) + "\n", encoding="utf-8")

            for final_path in (self.paths.index_file, self.paths.meta_file, self.paths.function_file, self.paths.offset_file, self.paths.config_file):
                if final_path.exists():
                    final_path.unlink()
            shutil.move(str(tmp_index), str(self.paths.index_file))
            shutil.move(str(tmp_meta), str(self.paths.meta_file))
            shutil.move(str(tmp_functions), str(self.paths.function_file))
            shutil.move(str(tmp_offsets), str(self.paths.offset_file))
            shutil.move(str(tmp_config), str(self.paths.config_file))
            self.index = None
            self.meta = []
            self.offsets = None
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    def _read_function_by_index(self, idx: int) -> FunctionInput:
        if self.offsets is None:
            raise RuntimeError("reference offsets are not loaded")
        offset = int(self.offsets[idx])
        with self.paths.function_file.open("r", encoding="utf-8") as fp:
            fp.seek(offset)
            line = fp.readline()
        if not line:
            raise RuntimeError(f"missing reference function at index {idx}")
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise RuntimeError(f"reference function at index {idx} is not an object")
        return obj

    def scan(
        self,
        target_records: Sequence[Dict[str, Any]],
        top_k: int,
        rerank_top_k: int,
        max_length: int,
        pad_to_multiple_of: int,
        only_marked_vulnerable: bool = False,
    ) -> List[Dict[str, Any]]:
        self.ensure_loaded()
        if self.index is None:
            raise RuntimeError("reference index is not loaded")
        if not target_records:
            return []
        target_functions = [record["function"] for record in target_records]
        target_embeddings = self.engine.embed(
            target_functions,
            encoder="query",
            max_length=max_length,
            pad_to_multiple_of=pad_to_multiple_of,
        )
        query = np.asarray(target_embeddings, dtype=np.float32)
        k = min(max(top_k, 1), int(self.index.ntotal))
        scores, indices = self.index.search(query, k)
        results: List[Dict[str, Any]] = []
        for target_idx, target_record in enumerate(target_records):
            target_meta = dict(target_record.get("meta", {}))
            target_function = target_record["function"]
            raw_candidates: List[Tuple[int, float]] = []
            for ref_idx, score in zip(indices[target_idx].tolist(), scores[target_idx].tolist()):
                if ref_idx < 0:
                    continue
                ref_meta = self.meta[int(ref_idx)]
                if only_marked_vulnerable and not bool(ref_meta.get("is_marked_vulnerable_function")):
                    continue
                raw_candidates.append((int(ref_idx), float(score)))
            compare_count = min(max(rerank_top_k, 0), len(raw_candidates))
            compare_scores: List[Optional[float]] = [None] * len(raw_candidates)
            if compare_count > 0:
                pairs = []
                for ref_idx, _ in raw_candidates[:compare_count]:
                    pairs.append({"left": target_function, "right": self._read_function_by_index(ref_idx)})
                pair_scores = self.engine.compare(pairs, max_length=max_length, pad_to_multiple_of=pad_to_multiple_of)
                for i, value in enumerate(pair_scores):
                    compare_scores[i] = float(value)
            matches: List[Dict[str, Any]] = []
            for (ref_idx, retrieval_score), comparison_score in zip(raw_candidates, compare_scores):
                ref_meta = dict(self.meta[ref_idx])
                matches.append({
                    "retrieval_score": retrieval_score,
                    "comparison_score": comparison_score,
                    "reference": {
                        "package": ref_meta.get("package", ""),
                        "version": ref_meta.get("version", "unknown"),
                        "cve": ref_meta.get("cve", ""),
                        "func_name": ref_meta.get("func_name", ""),
                        "file_path": ref_meta.get("file_path", ref_meta.get("path", "")),
                        "address": ref_meta.get("address", ""),
                        "arch": ref_meta.get("arch", ""),
                        "compiler": ref_meta.get("compiler", ""),
                        "optimizer": ref_meta.get("opt", ref_meta.get("optimizer", "")),
                        "dataset_file": ref_meta.get("dataset_file", ""),
                    },
                    "vulnerability": {
                        "cve": ref_meta.get("cve", ""),
                        "scope": "function" if bool(ref_meta.get("is_marked_vulnerable_function")) else "package_or_pool",
                        "is_marked_vulnerable_function": bool(ref_meta.get("is_marked_vulnerable_function")),
                        "marked_vulnerable_functions_for_cve": ref_meta.get("marked_vulnerable_functions_for_cve", []),
                    },
                })
            results.append({"kind": "scan_result", "target": target_meta, "matches": matches})
        return results
