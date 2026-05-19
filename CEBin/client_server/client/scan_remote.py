#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, Iterator, List, Optional, Tuple

import requests

try:
    import binaryninja as bn
except ImportError as exc:
    raise SystemExit(
        "BinaryNinja Python API is required on the client machine. "
        "Install/activate the Python environment where `import binaryninja` works."
    ) from exc


SKIP_FUNCTIONS = {
    "_init",
    "_start",
    "_dl_relocate_static_pie",
    "deregister_tm_clones",
    "register_tm_clones",
    "__do_global_dtors_aux",
    "frame_dummy",
    "__libc_csu_init",
    "__libc_csu_fini",
    "_fini",
}

ADDRESS_TOKEN_TYPES = {
    bn.InstructionTextTokenType.PossibleAddressToken,
    bn.InstructionTextTokenType.CodeRelativeAddressToken,
}


def merge_index_address_tokens(tokens: List[str]) -> List[str]:
    if len(tokens) <= 2:
        return tokens
    result = tokens[:]
    i = 0
    while i < len(result) - 2:
        if result[i + 1] == "@" and result[i + 2].startswith("0x") and result[i].isdigit():
            result = result[:i] + [result[i] + result[i + 1] + result[i + 2]] + result[i + 3:]
        i += 1
    return result


def function_mlil(bv, func) -> Tuple[int, int, Dict[str, List[str]]]:
    bb_cnt = 0
    instr_cnt = 0
    func_data: Dict[str, List[str]] = {}
    for block in func.medium_level_il:
        for instr in block:
            symbolized_tokens: List[str] = []
            index_address = f"{instr.instr_index}@{hex(instr.address)}"
            for token in instr.tokens:
                if token.type in ADDRESS_TOKEN_TYPES:
                    symbol = bv.get_symbol_at(token.value)
                    if symbol is not None:
                        symbolized_tokens.append(symbol.name.strip())
                    else:
                        symbolized_tokens.append(token.text.strip())
                else:
                    symbolized_tokens.append(token.text.strip())
            func_data[index_address] = merge_index_address_tokens(symbolized_tokens)
            instr_cnt += 1
        bb_cnt += 1
    return bb_cnt, instr_cnt, func_data


def derive_package(binary_path: str, package: str) -> str:
    if package != "auto":
        return package
    name = os.path.basename(binary_path)
    return os.path.splitext(name)[0] or name


def extract_binary(
    binary_path: str,
    package: str,
    arch: str,
    compiler: str,
    optimizer: str,
    binary_name: Optional[str],
    worker_threads: int,
    max_functions: Optional[int],
) -> Iterator[dict]:
    if worker_threads > 0:
        bn.set_worker_thread_count(worker_threads)
    name = binary_name if binary_name is not None else os.path.basename(binary_path)
    resolved_package = derive_package(binary_path, package)
    with bn.open_view(binary_path, update_analysis=False) as bv:
        bv.update_analysis_and_wait()
        resolved_arch = bv.arch.name if arch == "auto" and bv.arch is not None else arch
        count = 0
        for func_sym in bv.get_symbols_of_type(bn.SymbolType.FunctionSymbol):
            if func_sym.name in SKIP_FUNCTIONS:
                continue
            func = bv.get_function_at(func_sym.address)
            if func is None:
                continue
            try:
                bb_cnt, instr_cnt, func_data = function_mlil(bv, func)
                if not func_data:
                    continue
                yield {
                    "meta": {
                        "binary_path": binary_path,
                        "package": resolved_package,
                        "arch": resolved_arch,
                        "compiler": compiler,
                        "optimizer": optimizer,
                        "binary": name,
                        "func_name": func_sym.name,
                        "func_addr": hex(func_sym.address),
                        "bb_cnt": bb_cnt,
                        "instr_cnt": instr_cnt,
                    },
                    "function": func_data,
                }
                count += 1
                if max_functions is not None and count >= max_functions:
                    return
            except Exception as exc:
                print(f"[ERROR] {binary_path} {func_sym.name}: {exc}", file=sys.stderr)
                continue


def post_scan(args: argparse.Namespace, records: List[dict]) -> dict:
    payload = {
        "records": records,
        "top_k": args.top_k,
        "rerank_top_k": args.rerank_top_k,
        "max_length": args.max_length,
        "pad_to_multiple_of": 8,
        "only_marked_vulnerable": args.only_marked_vulnerable,
        "rebuild_index": args.rebuild_index,
    }
    if args.max_reference_functions is not None:
        payload["max_reference_functions"] = args.max_reference_functions
    response = requests.post(args.server.rstrip("/") + "/v1/scan", json=payload, timeout=None)
    if response.status_code != 200:
        raise RuntimeError(f"scan failed: HTTP {response.status_code}: {response.text}")
    return response.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan one local binary through a remote CEBin CVE reference server.")
    parser.add_argument("--input", required=True, help="Local binary path. This machine must have BinaryNinja Python API.")
    parser.add_argument("--server", default="http://127.0.0.1:9088")
    parser.add_argument("--package", default="auto")
    parser.add_argument("--arch", default="auto")
    parser.add_argument("--compiler", default="unknown")
    parser.add_argument("--optimizer", default="unknown")
    parser.add_argument("--binary-name")
    parser.add_argument("--worker-threads", type=int, default=2)
    parser.add_argument("--max-target-functions", type=int, help="Limit functions extracted from the target binary.")
    parser.add_argument("--limit", type=int, help="Alias for --max-target-functions.")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--rerank-top-k", type=int, default=5)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--only-marked-vulnerable", action="store_true")
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--max-reference-functions", type=int, help="Debug only: build index from the first N reference functions.")
    parser.add_argument("--raw", action="store_true", help="Print the whole server response as one JSON object.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_functions = args.max_target_functions if args.max_target_functions is not None else args.limit
    binary_path = os.path.abspath(args.input)
    print(json.dumps({"kind": "extract_start", "binary": binary_path}, ensure_ascii=False), flush=True)
    records = list(extract_binary(
        binary_path=binary_path,
        package=args.package,
        arch=args.arch,
        compiler=args.compiler,
        optimizer=args.optimizer,
        binary_name=args.binary_name,
        worker_threads=args.worker_threads,
        max_functions=max_functions,
    ))
    if not records:
        raise SystemExit("No functions were extracted.")
    print(json.dumps({"kind": "extract_done", "records": len(records)}, ensure_ascii=False), flush=True)
    data = post_scan(args, records)
    if args.raw:
        print(json.dumps(data, ensure_ascii=False))
        return
    for result in data.get("results", []):
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    print(json.dumps({
        "kind": "done",
        "mode": "scan",
        "records": len(data.get("results", [])),
        "reference_records": data.get("reference_records"),
    }, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
