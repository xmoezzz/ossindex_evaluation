from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .datastore import DataStore


THETA1 = 0.003
THETA3 = 0.8
MIN_COMMON_FUNCTIONS = 3
MIN_SCORE_DIFFERENCE = 0.1


@dataclass
class DetectRequest:
    input_name: str
    hashes: Dict[str, List[str]]
    include_vulnerabilities: bool = False


@dataclass
class ComponentMatch:
    component: str
    component_sig: str
    predicted_version: str
    newest_version: str
    time_diff_seconds: float
    matched_function_count: int
    total_function_count: float
    matched_ratio: float
    matched_hashes: List[str] = field(default_factory=list)
    matched_paths: List[str] = field(default_factory=list)
    vulnerabilities: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class DetectResult:
    input_name: str
    matches: List[ComponentMatch] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "input_name": self.input_name,
            "matches": [asdict(match) for match in self.matches],
            "warnings": list(self.warnings),
        }


class Detector:
    def __init__(self, store: DataStore) -> None:
        self.store = store

    def detect(self, request: DetectRequest) -> DetectResult:
        warnings: List[str] = []
        input_hash_paths = self._normalize_hash_paths(request.hashes)
        results = self._find_candidate_components(input_hash_paths, warnings)
        results = self._remove_same_path_candidates(request.input_name, results, input_hash_paths)

        cve_warning_added = False
        matches: List[ComponentMatch] = []
        for component in sorted(results.keys()):
            repo_name = self.store.repo_name_from_component(component)
            matched_hashes = results[component]
            try:
                version, newest_version, time_diff = self._predict_version(component, repo_name, matched_hashes, warnings)
            except FileNotFoundError as exc:
                warnings.append(f"missing metadata for {repo_name}: {exc}")
                continue
            except Exception as exc:
                warnings.append(f"failed to predict version for {repo_name}: {exc}")
                continue

            vulnerabilities: List[Dict[str, object]] = []
            if request.include_vulnerabilities:
                try:
                    vulnerabilities = [dict(item) for item in self.store.find_vulnerabilities(repo_name, version)]
                except FileNotFoundError as exc:
                    if not cve_warning_added:
                        warnings.append(str(exc))
                        cve_warning_added = True
                except Exception as exc:
                    if not cve_warning_added:
                        warnings.append(f"failed to query CVE database: {exc}")
                        cve_warning_added = True

            total_funcs = float(self.store.ave_funcs.get(repo_name, 0.0))
            matched_paths = self._paths_for_hashes(matched_hashes, input_hash_paths)
            matches.append(
                ComponentMatch(
                    component=repo_name,
                    component_sig=component,
                    predicted_version=str(version),
                    newest_version=str(newest_version),
                    time_diff_seconds=float(time_diff),
                    matched_function_count=len(matched_hashes),
                    total_function_count=total_funcs,
                    matched_ratio=(len(matched_hashes) / total_funcs) if total_funcs else 0.0,
                    matched_hashes=list(matched_hashes),
                    matched_paths=matched_paths,
                    vulnerabilities=vulnerabilities,
                )
            )

        matches.sort(key=lambda item: (item.matched_function_count, item.matched_ratio, item.component), reverse=True)
        return DetectResult(input_name=request.input_name, matches=matches, warnings=warnings)

    @staticmethod
    def _normalize_hash_paths(hashes: Mapping[str, Sequence[str] | str]) -> Dict[str, List[str]]:
        normalized: Dict[str, List[str]] = {}
        for hash_value, paths in hashes.items():
            if not hash_value:
                continue
            if isinstance(paths, str):
                normalized[str(hash_value)] = [paths]
            else:
                normalized[str(hash_value)] = [str(path) for path in paths]
        return normalized

    def _find_candidate_components(
        self,
        input_hash_paths: Mapping[str, Sequence[str]],
        warnings: List[str],
    ) -> Dict[str, List[str]]:
        common: Dict[str, List[str]] = {}
        for hash_value in input_hash_paths.keys():
            for component in self.store.hash_to_components.get(hash_value, []):
                common.setdefault(component, []).append(hash_value)

        results: Dict[str, List[str]] = {}
        for component, matched_hashes in common.items():
            repo_name = self.store.repo_name_from_component(component)
            total_funcs = self.store.ave_funcs.get(repo_name)
            if total_funcs is None:
                warnings.append(f"missing aveFuncs entry for {repo_name}")
                continue
            total_funcs = float(total_funcs)
            if total_funcs == 0.0:
                continue

            common_count = float(len(matched_hashes))
            ratio = common_count / total_funcs
            if ((common_count > 5 and ratio >= THETA1) or (common_count <= 5 and ratio >= THETA3)) and common_count >= MIN_COMMON_FUNCTIONS:
                results[component] = matched_hashes
        return results

    def _remove_same_path_candidates(
        self,
        input_name: str,
        results: Dict[str, List[str]],
        input_hash_paths: Mapping[str, Sequence[str]],
    ) -> Dict[str, List[str]]:
        input_component = self.store.component_from_repo_name(input_name)
        component_paths: Dict[str, Set[str]] = {
            component: set(self._paths_for_hashes(hashes, input_hash_paths))
            for component, hashes in results.items()
        }

        keys_to_remove: Set[str] = set()
        components = list(component_paths.keys())
        for component1 in components:
            if input_component == component1:
                continue
            for component2 in components:
                if input_component == component2 or component1 == component2:
                    continue
                if component_paths.get(component1) == component_paths.get(component2):
                    if len(results.get(component2, [])) < len(results.get(component1, [])):
                        keys_to_remove.add(component2)
                    else:
                        keys_to_remove.add(component1)
        return {component: hashes for component, hashes in results.items() if component not in keys_to_remove}

    @staticmethod
    def _paths_for_hashes(hashes: Sequence[str], input_hash_paths: Mapping[str, Sequence[str]]) -> List[str]:
        paths: List[str] = []
        for hash_value in hashes:
            paths.extend(str(path) for path in input_hash_paths.get(hash_value, []))
        return paths

    def _predict_version(
        self,
        component: str,
        repo_name: str,
        matched_hashes: Sequence[str],
        warnings: List[str],
    ) -> Tuple[str, str, float]:
        all_versions, idx_to_ver = self.store.read_all_versions(repo_name)
        version_scores: Dict[str, float] = {version: 0.0 for version in all_versions}
        weights = self.store.read_weights(repo_name)
        matched_hash_set = set(matched_hashes)

        for row in self.store.read_initial_sigs(component):
            hash_value = row.get("hash")
            if hash_value not in matched_hash_set:
                continue
            weight = weights.get(str(hash_value), 0.0)
            vers = row.get("vers", [])
            if not isinstance(vers, list):
                continue
            for version_idx in vers:
                version = idx_to_ver.get(version_idx)
                if version is None:
                    version = idx_to_ver.get(str(version_idx))
                if version is None:
                    continue
                version_scores[version] = version_scores.get(version, 0.0) + weight

        sorted_by_weight = sorted(version_scores.items(), key=lambda item: item[1], reverse=True)
        baseline_version: Optional[str] = None
        baseline_score = 0.0
        for version, score in sorted_by_weight:
            if version != "":
                baseline_version = version
                baseline_score = score
                break

        if baseline_version is None:
            return "master", "master", 0.0

        filtered_versions = [
            version
            for version, score in sorted_by_weight
            if version != baseline_version and version != "" and baseline_score - score < MIN_SCORE_DIFFERENCE
        ]

        selected_version = baseline_version
        selected_date = None
        newest_version = baseline_version
        newest_date = None

        release_dates = self.store.read_tag_dates(repo_name)

        if release_dates:
            newest_version = next(iter(release_dates.keys()))
            newest_date = release_dates[newest_version]
            selected_date = release_dates.get(baseline_version)

        for version in filtered_versions:
            version_date = release_dates.get(str(version))
            if version_date is None:
                continue
            if selected_date is None or version_date > selected_date:
                selected_version = str(version)
                selected_date = version_date

        if newest_date is not None and selected_date is not None:
            time_diff = (newest_date - selected_date).total_seconds()
        else:
            newest_version = selected_version
            time_diff = 0.0

        return selected_version, newest_version, time_diff
