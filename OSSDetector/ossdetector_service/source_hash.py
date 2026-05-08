from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple


def compute_tlsh(text: str) -> str:
    try:
        import tlsh  # type: ignore
    except ImportError as exc:
        raise RuntimeError("py-tlsh is required for source hashing") from exc
    digest = tlsh.forcehash(text.encode())
    if len(digest) == 72 and digest.startswith("T1"):
        return digest[2:]
    if digest in {"TNULL", "", "NULL"}:
        return ""
    return digest


def remove_comment(text: str) -> str:
    c_regex = re.compile(
        r'(?P<comment>//.*?$|[{}]+)|(?P<multilinecomment>/\*.*?\*/)|(?P<noncomment>\'(\\.|[^\\\'])*\'|"(\\.|[^\\"])*"|.[^/\'"]*)',
        re.DOTALL | re.MULTILINE,
    )
    return "".join(match.group("noncomment") for match in c_regex.finditer(text) if match.group("noncomment"))


def normalize(text: str) -> str:
    return "".join(text.replace("\n", "").replace("\r", "").replace("\t", "").replace("{", "").replace("}", "").split(" ")).lower()


def hash_source_tree(source_dir: str | Path, ctags_path: str = "/usr/local/bin/ctags") -> Tuple[Dict[str, List[str]], int, int, int]:
    root = Path(source_dir).resolve()
    possible = {".c", ".cc", ".cpp"}
    result: Dict[str, List[str]] = {}
    file_count = 0
    function_count = 0
    line_count = 0

    func_pattern = re.compile(r"(function)")
    number_pattern = re.compile(r"(\d+)")
    body_pattern = re.compile(r"{([\S\s]*)}")

    for file_path in root.rglob("*"):
        if not file_path.is_file() or file_path.suffix not in possible:
            continue
        try:
            output = subprocess.check_output(
                [ctags_path, "-f", "-", "--kinds-C=*", "--fields=neKSt", str(file_path)],
                stderr=subprocess.STDOUT,
            ).decode()
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
            file_count += 1
            line_count += len(lines)
            for raw in output.split("\n"):
                elem = re.sub(r"[\t\s ]{2,}", "", raw).split("\t")
                if raw == "" or len(elem) < 8 or not func_pattern.fullmatch(elem[3]):
                    continue
                start_match = number_pattern.search(elem[4])
                end_match = number_pattern.search(elem[7])
                if start_match is None or end_match is None:
                    continue
                start_line = int(start_match.group(0))
                end_line = int(end_match.group(0))
                func_text = "".join(lines[start_line - 1 : end_line])
                body_match = body_pattern.search(func_text)
                body = body_match.group(1) if body_match else " "
                digest = compute_tlsh(normalize(remove_comment(body)))
                if not digest:
                    continue
                stored_path = "/" + str(file_path.relative_to(root))
                result.setdefault(digest, []).append(stored_path)
                function_count += 1
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ctags failed for {file_path}: {exc.output.decode(errors='ignore')}") from exc
    return result, file_count, function_count, line_count
