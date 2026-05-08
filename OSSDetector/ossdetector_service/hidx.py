from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def parse_hidx_lines(lines: Iterable[str]) -> Dict[str, List[str]]:
    """Parse OSSDetector .hidx lines into hash -> paths.

    The original detector skips the first line. Sample files use the first line
    for project-level counters rather than a function hash.
    """
    hash_paths: Dict[str, List[str]] = {}
    first = True
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if first:
            first = False
            continue
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise ValueError(f"invalid hidx line: {line!r}")
        hash_value = parts[0].strip()
        path = parts[1].strip()
        if not hash_value:
            raise ValueError(f"empty hash in hidx line: {line!r}")
        hash_paths.setdefault(hash_value, []).append(path)
    return hash_paths


def read_hidx(path: str | Path) -> Dict[str, List[str]]:
    with Path(path).open("r", encoding="utf-8") as fp:
        return parse_hidx_lines(fp)


def infer_input_name_from_hidx(path: str | Path) -> str:
    name = Path(path).name
    if "_fuzzy" in name:
        return name.split("_fuzzy", 1)[0]
    return Path(name).stem


def iter_hidx_files(input_dir: str | Path) -> Iterable[Path]:
    base = Path(input_dir)
    for path in sorted(base.iterdir()):
        if path.is_file() and path.name.endswith(".hidx"):
            yield path
