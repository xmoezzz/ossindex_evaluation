from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

from .datastore import DataLayout, DataStore
from .detector import DetectRequest, Detector
from .hidx import infer_input_name_from_hidx, iter_hidx_files, read_hidx
from .source_hash import hash_source_tree
from .data_init import initialize_data

DEFAULT_ARCHIVES_DIR = "data"
DEFAULT_DATA_DIR = "data/ossdetector"


def build_detector(data_dir: str) -> Detector:
    return Detector(DataStore(data_dir))


def command_detect_hidx(args: argparse.Namespace) -> None:
    detector = build_detector(args.data_dir)
    input_name = args.input_name or infer_input_name_from_hidx(args.input)
    hash_paths = read_hidx(args.input)
    result = detector.detect(DetectRequest(input_name=input_name, hashes=hash_paths))
    payload = result.to_dict()
    if args.output_json:
        output = Path(args.output_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def command_detect_hidx_dir(args: argparse.Namespace) -> None:
    detector = build_detector(args.data_dir)
    output = Path(args.output_jsonl)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as out:
        for hidx_path in iter_hidx_files(args.input_dir):
            input_name = infer_input_name_from_hidx(hidx_path)
            try:
                hash_paths = read_hidx(hidx_path)
                result = detector.detect(DetectRequest(input_name=input_name, hashes=hash_paths))
                payload = result.to_dict()
            except Exception as exc:
                payload = {"input_name": input_name, "matches": [], "warnings": [str(exc)]}
            out.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def command_hash_source(args: argparse.Namespace) -> None:
    hash_paths, file_count, function_count, line_count = hash_source_tree(args.source_dir, ctags_path=args.ctags_path)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fp:
        fp.write(f"{Path(args.source_dir).name}\t{file_count}\t{function_count}\t{line_count}\n")
        for hash_value in sorted(hash_paths.keys()):
            for path in hash_paths[hash_value]:
                fp.write(f"{hash_value}\t{path}\n")


def command_init_data(args: argparse.Namespace) -> None:
    linked = initialize_data(
        args.archives_dir,
        args.data_dir,
        force=args.force,
        copy=args.copy,
        keep_extracted=not args.remove_extracted,
    )
    status = linked.get("status")
    for name, source in sorted(linked.items()):
        print(f"{name}: {source}")
    if status in {"already_initialized", "already_complete"}:
        print("OSSDetector data initialization skipped.")
    else:
        print("OSSDetector data initialization completed.")


def command_stats(args: argparse.Namespace) -> None:
    layout = DataLayout.from_data_dir(args.data_dir)
    required_paths = {
        "componentDB_ours_6.0": layout.component_db_path,
        "initialSigs_ours": layout.initial_db_path,
        "metaInfos_ours_6.0": layout.meta_path,
        "aveFuncs": layout.ave_func_path,
        "weights_ours_6.0": layout.weight_path,
        "verIDX_ours": layout.ver_idx_path,
    }
    payload = {
        "data_dir": str(layout.data_dir),
        "layout_ok": all(path.exists() for path in required_paths.values()),
        "required_paths": {name: str(path) for name, path in required_paths.items()},
        "missing_paths": [str(path) for path in required_paths.values() if not path.exists()],
        "component_db_files": sum(1 for path in layout.component_db_path.iterdir() if path.is_file()) if layout.component_db_path.is_dir() else 0,
        "initial_sig_files": sum(1 for path in layout.initial_db_path.iterdir() if path.is_file()) if layout.initial_db_path.is_dir() else 0,
        "weight_files": sum(1 for path in layout.weight_path.iterdir() if path.is_file()) if layout.weight_path.is_dir() else 0,
        "ver_idx_files": sum(1 for path in layout.ver_idx_path.iterdir() if path.is_file()) if layout.ver_idx_path.is_dir() else 0,
        "repo_date_available": layout.date_path.is_dir(),
        "cve_version_path": str(layout.cve_version_path),
        "cve_version_available": layout.cve_version_path.is_file(),
    }
    if args.full:
        store = DataStore(args.data_dir)
        payload["full"] = store.stats()
    print(json.dumps(payload, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="OSSDetector service CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect_one = subparsers.add_parser("detect-hidx", help="Detect components from one .hidx file")
    detect_one.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    detect_one.add_argument("--input", required=True)
    detect_one.add_argument("--input-name")
    detect_one.add_argument("--output-json")
    detect_one.set_defaults(func=command_detect_hidx)

    detect_dir = subparsers.add_parser("detect-hidx-dir", help="Detect components from every .hidx file in a directory")
    detect_dir.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    detect_dir.add_argument("--input-dir", required=True)
    detect_dir.add_argument("--output-jsonl", required=True)
    detect_dir.set_defaults(func=command_detect_hidx_dir)

    hash_source = subparsers.add_parser("hash-source", help="Generate a .hidx file from a C/C++ source tree")
    hash_source.add_argument("--source-dir", required=True)
    hash_source.add_argument("--output", required=True)
    hash_source.add_argument("--ctags-path", default="/usr/local/bin/ctags")
    hash_source.set_defaults(func=command_hash_source)


    init_data = subparsers.add_parser("init-data", help="Extract Zenodo archives and create the required data layout")
    init_data.add_argument("--archives-dir", default=DEFAULT_ARCHIVES_DIR)
    init_data.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    init_data.add_argument("--force", action="store_true")
    init_data.add_argument("--copy", action="store_true", help="Copy payload directories instead of creating symlinks")
    init_data.add_argument("--remove-extracted", action="store_true", help="Remove _extracted after copying. Requires --copy")
    init_data.set_defaults(func=command_init_data)

    stats = subparsers.add_parser("stats", help="Print data layout statistics without loading componentDB")
    stats.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    stats.add_argument("--full", action="store_true", help="Also load componentDB and print full in-memory index statistics")
    stats.set_defaults(func=command_stats)

    args = parser.parse_args()
    if getattr(args, "remove_extracted", False) and not getattr(args, "copy", False):
        parser.error("--remove-extracted requires --copy")
    args.func(args)


if __name__ == "__main__":
    main()
