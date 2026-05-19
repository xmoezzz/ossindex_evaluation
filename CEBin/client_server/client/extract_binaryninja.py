#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

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


def add_cebin_path(cebin_root: str) -> None:
    finetune_dir = os.path.join(os.path.abspath(cebin_root), "finetune")
    if not os.path.isdir(finetune_dir):
        raise FileNotFoundError(finetune_dir)
    if finetune_dir not in sys.path:
        sys.path.insert(0, finetune_dir)


def load_tokenizer(cebin_root: str, tokenizer_path: str, max_length: int):
    add_cebin_path(cebin_root)
    from tokenizer import CebinTokenizer

    tokenizer = CebinTokenizer.from_pretrained(tokenizer_path)
    tokenizer.max_length = max_length
    tokenizer.max_len = max_length
    return tokenizer


def iter_binary_paths(binary: Optional[str], binary_list: Optional[str]) -> Iterator[str]:
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


def extract_binary(
    binary_path: str,
    tokenizer,
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
    with bn.open_view(binary_path, update_analysis=False) as bv:
        bv.update_analysis_and_wait()
        count = 0
        for func_sym in bv.get_symbols_of_type(bn.SymbolType.FunctionSymbol):
            if func_sym.name in SKIP_FUNCTIONS:
                continue
            func = bv.get_function_at(func_sym.address)
            if func is None:
                continue
            try:
                bb_cnt, instr_cnt, func_data = function_mlil(bv, func)
                encoded = tokenizer.encode_function(func_data)
                if encoded is None:
                    continue
                yield {
                    "meta": {
                        "binary_path": binary_path,
                        "package": package,
                        "arch": arch,
                        "compiler": compiler,
                        "optimizer": optimizer,
                        "binary": name,
                        "func_name": func_sym.name,
                        "func_addr": hex(func_sym.address),
                        "bb_cnt": bb_cnt,
                        "instr_cnt": instr_cnt,
                    },
                    "function": {
                        "input_ids": encoded["input_ids"],
                        "attention_mask": encoded["attention_mask"],
                        "token_type_ids": encoded["token_type_ids"],
                    },
                    "cebin": {
                        "pad_token_id": tokenizer.pad_token_id,
                        "max_length": tokenizer.max_length,
                    },
                }
                count += 1
                if max_functions is not None and count >= max_functions:
                    return
            except Exception as exc:
                print(f"[ERROR] {binary_path} {func_sym.name}: {exc}", file=sys.stderr)
                continue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract CEBin tokenized functions with local BinaryNinja.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--binary", help="Single binary path.")
    src.add_argument("--binary-list", help="Text file with one binary path per line.")
    parser.add_argument("--cebin-root", required=True, help="Path to the CEBin repository root.")
    parser.add_argument("--tokenizer", required=True, help="Path to cebin-tokenizer directory.")
    parser.add_argument("--output", required=True, help="Output JSONL path.")
    parser.add_argument("--package", default="unknown")
    parser.add_argument("--arch", default="unknown")
    parser.add_argument("--compiler", default="unknown")
    parser.add_argument("--optimizer", default="unknown")
    parser.add_argument("--binary-name", help="Override binary name for --binary mode.")
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--worker-threads", type=int, default=2)
    parser.add_argument("--max-functions", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tokenizer = load_tokenizer(args.cebin_root, args.tokenizer, args.max_length)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    written = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for binary_path in iter_binary_paths(args.binary, args.binary_list):
            for record in extract_binary(
                binary_path=binary_path,
                tokenizer=tokenizer,
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
    print(f"wrote {written} functions to {args.output}")


if __name__ == "__main__":
    main()
