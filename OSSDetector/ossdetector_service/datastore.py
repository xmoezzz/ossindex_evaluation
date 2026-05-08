from __future__ import annotations

import datetime as dt
import json
import time
from json import JSONDecodeError
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class DataLayout:
    data_dir: Path
    component_db_path: Path
    initial_db_path: Path
    meta_path: Path
    ave_func_path: Path
    weight_path: Path
    ver_idx_path: Path
    date_path: Path
    cve_version_path: Path

    @staticmethod
    def from_data_dir(data_dir: str | Path) -> "DataLayout":
        root = Path(data_dir).expanduser().resolve()
        return DataLayout(
            data_dir=root,
            component_db_path=root / "componentDB_ours_6.0",
            initial_db_path=root / "initialSigs_ours",
            meta_path=root / "metaInfos_ours_6.0",
            ave_func_path=root / "metaInfos_ours_6.0" / "aveFuncs",
            weight_path=root / "metaInfos_ours_6.0" / "weights_ours_6.0",
            ver_idx_path=root / "verIDX_ours",
            date_path=root / "repo-date",
            cve_version_path=root / "cve-version.xlsx",
        )


class DataStore:
    """Read OSSDetector data and expose indexes for the detector.

    The original detector scans every component for every input. This store
    builds a reverse index once at startup: function hash -> component sig file.
    Version, weight, tag-date, and CVE files are read lazily and cached.
    """

    def __init__(
        self,
        data_dir: str | Path,
        strict_layout: bool = True,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
    ) -> None:
        self.layout = DataLayout.from_data_dir(data_dir)
        self._progress_callback = progress_callback
        self._report_progress(stage="init", message="starting DataStore initialization", data_dir=str(self.layout.data_dir))
        if strict_layout:
            self._report_progress(stage="validate_layout", message="validating OSSDetector data layout")
            self.validate_layout()

        self._report_progress(stage="read_ave_funcs", message="loading aveFuncs")
        self.ave_funcs: Dict[str, float] = self._read_ave_funcs()
        self._report_progress(stage="read_ave_funcs", message="loaded aveFuncs", ave_func_entries=len(self.ave_funcs))

        self.component_hashes: Dict[str, List[str]] = {}
        self.hash_to_components: DefaultDict[str, List[str]] = defaultdict(list)
        self.skipped_component_files: List[str] = []
        self._load_component_db()
        self._report_progress(stage="ready", message="DataStore initialization complete", **self.stats())

    def _report_progress(self, **event: object) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(dict(event))
        except Exception:
            pass

    def validate_layout(self) -> None:
        required_paths = [
            self.layout.component_db_path,
            self.layout.initial_db_path,
            self.layout.meta_path,
            self.layout.ave_func_path,
            self.layout.weight_path,
            self.layout.ver_idx_path,
        ]
        missing = [str(path) for path in required_paths if not path.exists()]
        if missing:
            joined = "\n".join(f"  - {path}" for path in missing)
            raise FileNotFoundError(f"OSSDetector data layout is incomplete:\n{joined}")

    @staticmethod
    def repo_name_from_component(component: str) -> str:
        if component.endswith("_sig"):
            return component[:-4]
        return component

    @staticmethod
    def component_from_repo_name(repo_name: str) -> str:
        if repo_name.endswith("_sig"):
            return repo_name
        return f"{repo_name}_sig"

    def stats(self) -> Dict[str, object]:
        return {
            "components": len(self.component_hashes),
            "component_hash_edges": sum(len(v) for v in self.component_hashes.values()),
            "unique_hashes": len(self.hash_to_components),
            "ave_func_entries": len(self.ave_funcs),
            "repo_date_available": self.layout.date_path.is_dir(),
            "cve_version_available": self.layout.cve_version_path.is_file(),
        }

    def _read_ave_funcs(self) -> Dict[str, float]:
        with self.layout.ave_func_path.open("r", encoding="utf-8", errors="replace") as fp:
            raw = json.load(fp)
        result: Dict[str, float] = {}
        for key, value in raw.items():
            try:
                result[key] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    def _load_component_db(self) -> None:
        paths = [path for path in sorted(self.layout.component_db_path.iterdir()) if path.is_file()]
        total = len(paths)
        self._report_progress(
            stage="load_component_db",
            message="loading componentDB_ours_6.0",
            total_files=total,
            processed_files=0,
            percent=0.0,
        )
        last_report = time.monotonic()

        for idx, path in enumerate(paths, start=1):
            component = path.name
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fp:
                    entries = json.load(fp)
            except (OSError, JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                self.skipped_component_files.append(component)
                continue

            if not isinstance(entries, list):
                self.skipped_component_files.append(component)
                continue

            hashes: List[str] = []
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                hash_value = entry.get("hash")
                if not isinstance(hash_value, str) or not hash_value:
                    continue
                hashes.append(hash_value)
                self.hash_to_components[hash_value].append(component)
            self.component_hashes[component] = hashes

            now = time.monotonic()
            if idx == total or idx % 100 == 0 or now - last_report >= 2.0:
                last_report = now
                percent = (idx / total * 100.0) if total else 100.0
                self._report_progress(
                    stage="load_component_db",
                    message="loading componentDB_ours_6.0",
                    total_files=total,
                    processed_files=idx,
                    percent=round(percent, 2),
                    components=len(self.component_hashes),
                    unique_hashes=len(self.hash_to_components),
                    skipped_files=len(self.skipped_component_files),
                    current_file=component,
                )

    @lru_cache(maxsize=8192)
    def read_all_versions(self, repo_name: str) -> Tuple[List[str], Dict[object, str]]:
        path = self.layout.ver_idx_path / f"{repo_name}_idx"
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            rows = json.load(fp)
        all_versions: List[str] = []
        idx_to_ver: Dict[object, str] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            version = str(row.get("ver", ""))
            idx = row.get("idx")
            all_versions.append(version)
            idx_to_ver[idx] = version
            idx_to_ver[str(idx)] = version
        return all_versions, idx_to_ver

    @lru_cache(maxsize=8192)
    def read_weights(self, repo_name: str) -> Dict[str, float]:
        path = self.layout.weight_path / f"{repo_name}_weights"
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            raw = json.load(fp)
        result: Dict[str, float] = {}
        for key, value in raw.items():
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    @lru_cache(maxsize=8192)
    def read_initial_sigs(self, component: str) -> List[Mapping[str, object]]:
        path = self.layout.initial_db_path / component
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            rows = json.load(fp)
        return [row for row in rows if isinstance(row, Mapping)]

    @lru_cache(maxsize=8192)
    def read_tag_dates(self, repo_name: str) -> Dict[str, dt.datetime]:
        if not self.layout.date_path.is_dir():
            return {}

        path = self.layout.date_path / repo_name
        if not path.is_file():
            return {}

        tag_dates: Dict[str, dt.datetime] = {}
        with path.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                parts = line.split("(", 1)
                if len(parts) <= 1:
                    continue
                date_str = parts[0].strip()
                tags_str = parts[1].replace(")", "")
                try:
                    date_obj = dt.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
                except ValueError:
                    continue
                for tag_part in tags_str.split(", "):
                    if "tag" not in tag_part:
                        continue
                    tag = tag_part.strip().replace("tag:", "").strip()
                    if tag:
                        tag_dates[tag] = date_obj
        return tag_dates

    @staticmethod
    def _normalize_excel_value(value: Any) -> object:
        if value is None:
            return None
        try:
            import pandas as pd  # type: ignore
            if pd.isna(value):
                return None
        except Exception:
            pass
        return value

    @staticmethod
    def _parse_original_versions_cell(value: Any) -> Tuple[str, List[str]]:
        normalized = DataStore._normalize_excel_value(value)
        versions_raw = "" if normalized is None else str(normalized)
        versions_list = versions_raw.strip("[]").replace("'", "").split(", ")
        return versions_raw, versions_list

    @staticmethod
    def _cve_row_matches_version(repo_name: str, version: str, versions_raw: str, versions_list: List[str]) -> bool:
        return version in versions_list or (version == repo_name and versions_raw == "[]")

    @lru_cache(maxsize=1)
    def read_cve_rows(self) -> List[Dict[str, object]]:
        path = self.layout.cve_version_path
        if not path.is_file():
            raise FileNotFoundError(f"OSSDetector CVE database not found: {path}")

        try:
            import pandas as pd  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "pandas and an Excel reader are required to read cve-version.xlsx. "
                "Install pandas and openpyxl in this Python environment."
            ) from exc

        self._report_progress(stage="read_cve", message="loading cve-version.xlsx", path=str(path))
        df = pd.read_excel(path)
        required = {"repoName", "versions", "cve_id", "cwe_id", "base_score"}
        missing = sorted(required - set(str(column) for column in df.columns))
        if missing:
            raise ValueError(f"cve-version.xlsx is missing required columns: {', '.join(missing)}")

        rows: List[Dict[str, object]] = []
        total_rows = len(df.index)
        last_report = time.monotonic()
        for idx, (_, row) in enumerate(df.iterrows(), start=1):
            repo_value = self._normalize_excel_value(row.get("repoName"))
            if repo_value is None:
                continue
            repo_name = str(repo_value)
            versions_raw, versions_list = self._parse_original_versions_cell(row.get("versions"))
            rows.append(
                {
                    "repoName": repo_name,
                    "versions": versions_raw,
                    "versions_list": versions_list,
                    "cve_id": self._normalize_excel_value(row.get("cve_id")),
                    "cwe_id": self._normalize_excel_value(row.get("cwe_id")),
                    "base_score": self._normalize_excel_value(row.get("base_score")),
                }
            )
            now = time.monotonic()
            if idx == total_rows or idx % 500 == 0 or now - last_report >= 2.0:
                last_report = now
                percent = (idx / total_rows * 100.0) if total_rows else 100.0
                self._report_progress(
                    stage="read_cve",
                    message="loading cve-version.xlsx",
                    total_rows=total_rows,
                    processed_rows=idx,
                    percent=round(percent, 2),
                    loaded_rows=len(rows),
                )
        self._report_progress(stage="read_cve", message="loaded cve-version.xlsx", loaded_rows=len(rows))
        return rows

    @lru_cache(maxsize=8192)
    def find_vulnerabilities(self, repo_name: str, version: str) -> Tuple[Dict[str, object], ...]:
        repo_name = self.repo_name_from_component(str(repo_name))
        version = str(version)
        matches: List[Dict[str, object]] = []
        for row in self.read_cve_rows():
            if row.get("repoName") != repo_name:
                continue
            versions_raw = str(row.get("versions", ""))
            versions_list_value = row.get("versions_list", [])
            versions_list = versions_list_value if isinstance(versions_list_value, list) else []
            if not self._cve_row_matches_version(repo_name, version, versions_raw, versions_list):
                continue
            matches.append(
                {
                    "repoName": repo_name,
                    "version": version,
                    "cve_id": row.get("cve_id"),
                    "cwe_id": row.get("cwe_id"),
                    "base_score": row.get("base_score"),
                    "affected_versions": versions_raw,
                }
            )
        return tuple(matches)
