from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .datastore import DataLayout


@dataclass(frozen=True)
class ArchiveSpec:
    key: str
    filename: str
    target_name: str


ARCHIVE_SPECS: List[ArchiveSpec] = [
    ArchiveSpec("component", "component.tar.gz", "componentDB_ours_6.0"),
    ArchiveSpec("initial", "initial.tar.gz", "initialSigs_ours"),
    ArchiveSpec("meta", "meta.tar.gz", "metaInfos_ours_6.0"),
    ArchiveSpec("ver", "ver.tar.gz", "verIDX_ours"),
]

INIT_MARKER = ".ossdetector_init.json"


class DataInitError(RuntimeError):
    pass


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise DataInitError(f"Unsafe path in archive {archive}: {member.name}") from exc
        tar.extractall(destination, members=members)


def _remove_existing(path: Path, force: bool) -> None:
    if not os.path.lexists(path):
        return
    if not force:
        raise DataInitError(f"Refusing to overwrite existing path without --force: {path}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _make_link_or_copy(source: Path, target: Path, force: bool, copy: bool) -> None:
    source = source.resolve()
    if os.path.lexists(target):
        try:
            if target.resolve() == source:
                return
        except FileNotFoundError:
            pass
    _remove_existing(target, force=force)
    if copy:
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    else:
        os.symlink(source, target, target_is_directory=source.is_dir())


def _children_dirs(root: Path) -> List[Path]:
    return [path for path in root.iterdir() if path.is_dir()]


def _find_named_dir(root: Path, name: str) -> Optional[Path]:
    direct = root / name
    if direct.is_dir():
        return direct
    for path in root.rglob(name):
        if path.is_dir():
            return path
    return None


def _single_payload_dir(root: Path) -> Optional[Path]:
    dirs = _children_dirs(root)
    files = [path for path in root.iterdir() if path.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return None


def _looks_like_component_db(path: Path) -> bool:
    if not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_file() and child.name.endswith("_sig"):
            return True
    return False


def _looks_like_initial_db(path: Path) -> bool:
    if not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_file() and child.name.endswith("_sig"):
            return True
    return False


def _looks_like_meta(path: Path) -> bool:
    return (path / "aveFuncs").is_file() and (path / "weights_ours_6.0").is_dir()


def _looks_like_ver_idx(path: Path) -> bool:
    if not path.is_dir():
        return False
    for child in path.iterdir():
        if child.is_file() and child.name.endswith("_idx"):
            return True
    return False


def _find_by_predicate(root: Path, predicate) -> Optional[Path]:
    if predicate(root):
        return root
    for path in root.rglob("*"):
        if path.is_dir() and predicate(path):
            return path
    return None


def _resolve_payload_dir(extract_root: Path, spec: ArchiveSpec) -> Path:
    named = _find_named_dir(extract_root, spec.target_name)
    if named is not None:
        return named

    payload = _single_payload_dir(extract_root)
    if payload is not None:
        if spec.key == "meta" and _looks_like_meta(payload):
            return payload
        if spec.key == "ver" and _looks_like_ver_idx(payload):
            return payload
        if spec.key in {"component", "initial"}:
            return payload

    if spec.key == "component":
        found = _find_by_predicate(extract_root, _looks_like_component_db)
    elif spec.key == "initial":
        found = _find_by_predicate(extract_root, _looks_like_initial_db)
    elif spec.key == "meta":
        found = _find_by_predicate(extract_root, _looks_like_meta)
    elif spec.key == "ver":
        found = _find_by_predicate(extract_root, _looks_like_ver_idx)
    else:
        found = None

    if found is None:
        raise DataInitError(f"Cannot identify payload directory for {spec.filename} under {extract_root}")
    return found


def _layout_required_paths(data_root: Path) -> List[Path]:
    layout = DataLayout.from_data_dir(data_root)
    return [
        layout.component_db_path,
        layout.initial_db_path,
        layout.meta_path,
        layout.ave_func_path,
        layout.weight_path,
        layout.ver_idx_path,
    ]


def _layout_missing_paths(data_root: Path) -> List[Path]:
    return [path for path in _layout_required_paths(data_root) if not path.exists()]


def _layout_is_complete(data_root: Path) -> bool:
    return not _layout_missing_paths(data_root)


def _write_init_marker(data_root: Path, archives_root: Path, linked: Dict[str, str], copy: bool) -> None:
    payload = {
        "initialized_at_unix": int(time.time()),
        "archives_dir": str(archives_root),
        "data_dir": str(data_root),
        "copy": bool(copy),
        "linked": linked,
    }
    (data_root / INIT_MARKER).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_init_marker(data_root: Path) -> Optional[Dict[str, object]]:
    marker = data_root / INIT_MARKER
    if not marker.is_file():
        return None
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def initialize_data(
    archives_dir: str | Path,
    data_dir: str | Path,
    *,
    force: bool = False,
    copy: bool = False,
    keep_extracted: bool = True,
) -> Dict[str, str]:
    archives_root = Path(archives_dir).expanduser().resolve()
    data_root = Path(data_dir).expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True)

    if _layout_is_complete(data_root) and not force:
        marker = _read_init_marker(data_root)
        status = "already_initialized" if marker is not None else "already_complete"
        return {
            "status": status,
            "data_dir": str(data_root),
            "message": "OSSDetector data layout is already complete; skipped extraction. Use --force to rebuild.",
        }

    extracted_base = data_root / "_extracted"
    extracted_base.mkdir(parents=True, exist_ok=True)

    linked: Dict[str, str] = {}
    for spec in ARCHIVE_SPECS:
        archive = archives_root / spec.filename
        if not archive.is_file():
            raise DataInitError(f"Missing archive: {archive}")

        extract_root = extracted_base / spec.key
        if extract_root.exists():
            if force:
                shutil.rmtree(extract_root)
            else:
                payload = _resolve_payload_dir(extract_root, spec)
                target = data_root / spec.target_name
                _make_link_or_copy(payload, target, force=force, copy=copy)
                linked[spec.target_name] = str(payload)
                continue

        extract_root.mkdir(parents=True, exist_ok=True)
        print(f"extract {archive} -> {extract_root}", flush=True)
        _safe_extract_tar(archive, extract_root)
        payload = _resolve_payload_dir(extract_root, spec)
        target = data_root / spec.target_name
        _make_link_or_copy(payload, target, force=force, copy=copy)
        linked[spec.target_name] = str(payload)

    if not keep_extracted and copy:
        shutil.rmtree(extracted_base, ignore_errors=True)

    missing = _layout_missing_paths(data_root)
    if missing:
        detail = "\n".join(f"  - {path}" for path in missing)
        raise DataInitError(f"Initialized data layout is incomplete:\n{detail}")

    linked["status"] = "initialized"
    linked["data_dir"] = str(data_root)
    _write_init_marker(data_root, archives_root, linked, copy=copy)
    return linked
