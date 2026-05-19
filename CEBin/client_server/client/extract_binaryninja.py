#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Dict, Iterator, List, Optional, Tuple

try:
    import binaryninja as bn
except ImportError as exc:
    raise SystemExit("binaryninja Python module is required on the client machine.") from exc


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


def iter_binary_paths(inputs: List[str], binary: Optional[str], binary_list: Optional[str]) -> Iterator[str]:
    for path in inputs:
        yield os.path.abspath(path)
    if binary is not None:
        yield os.path.abspath(binary)
    if binary_list is not None:
        with open(binary_list, "r", encoding="utf-8") as fp:
            for line in fp:
                path = line.strip()
                if path:
                    yield os.path.abspath(path)


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


def derive_output_path(output: Optional[str], binaries: List[str]) -> str:
    if output:
        return output
    if len(binaries) == 1:
        base = os.path.basename(binaries[0])
        stem = os.path.splitext(base)[0] or base
        return os.path.abspath(f"{stem}.functions.jsonl")
    return os.path.abspath("functions.jsonl")


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract raw BinaryNinja MLIL tokens for remote CEBin inference.",
        epilog=(
            "Minimal usage: python client/extract_binaryninja.py /path/to/binary "
            "-o output.functions.jsonl"
        ),
    )
    parser.add_argument("inputs", nargs="*", help="Binary paths. Usually just pass one binary here.")
    parser.add_argument("--binary", help="Single binary path. Kept for compatibility.")
    parser.add_argument("--binary-list", help="Text file with one binary path per line.")
    parser.add_argument("-o", "--output", help="Output JSONL path. Default: ./<binary>.functions.jsonl")
    parser.add_argument("--package", default="auto", help="Metadata package name. Default: binary filename stem.")
    parser.add_argument("--arch", default="auto", help="Metadata architecture. Default: BinaryNinja view architecture.")
    parser.add_argument("--compiler", default="unknown")
    parser.add_argument("--optimizer", default="unknown")
    parser.add_argument("--binary-name", help="Override binary name for --binary mode.")
    parser.add_argument("--worker-threads", type=int, default=2)
    parser.add_argument("--max-functions", type=int)
    args = parser.parse_args()
    if not args.inputs and args.binary is None and args.binary_list is None:
        parser.error("pass a binary path, --binary, or --binary-list")
    return args


def main() -> None:
    args = parse_args()
    binaries = list(dict.fromkeys(iter_binary_paths(args.inputs, args.binary, args.binary_list)))
    if not binaries:
        raise SystemExit("No input binaries.")
    output = derive_output_path(args.output, binaries)

    print(f"[INFO] OUTPUT={output}")
    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    written = 0
    with open(output, "w", encoding="utf-8") as out:
        for binary_path in binaries:
            for record in extract_binary(
                binary_path=binary_path,
                package=args.package,
                arch=args.arch,
                compiler=args.compiler,
                optimizer=args.optimizer,
                binary_name=args.binary_name,
                worker_threads=args.worker_threads,
                max_functions=args.max_functions,
            ):
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
                written += 1
    if written == 0:
        raise SystemExit("No functions were extracted.")
    print(f"wrote {written} raw functions to {output}")


if __name__ == "__main__":
    main()
