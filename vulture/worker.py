from __future__ import annotations
import ast
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import zipfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
WORK = ROOT / 'work'
OUTPUT = ROOT / 'output'
VENDOR = ROOT / 'vendor'
VULTURE_REPO = VENDOR / 'Vulture'
VULTURE_PYTHON = os.environ.get('VULTURE_PYTHON', sys.executable)


class ServiceUnavailableError(RuntimeError):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


@dataclass
class Job:
    job_id: str
    request: Dict[str, Any]
    status: str = 'queued'
    created_at: str = field(default_factory=iso_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    workdir: str = ''
    outdir: str = ''
    pid: Optional[int] = None
    result_path: Optional[str] = None


class VultureService:
    def __init__(self) -> None:
        ensure_dir(DATA)
        ensure_dir(WORK)
        ensure_dir(OUTPUT)
        ensure_dir(VENDOR)
        self._jobs: Dict[str, Job] = {}
        self._queue: 'queue.Queue[str]' = queue.Queue()
        self._lock = threading.Lock()
        self._procs: Dict[str, subprocess.Popen] = {}
        self._init_lock = threading.Lock()
        self._init_event = threading.Event()
        self._init_status = 'new'
        self._init_error: Optional[str] = None
        self._init_started_at: Optional[str] = None
        self._init_finished_at: Optional[str] = None
        self._init_result: Dict[str, Any] = {}
        self._init_run_oneday = True
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def healthz(self) -> Dict[str, Any]:
        repo_ready = VULTURE_REPO.exists()
        layout = self._check_dataset_layout(run_oneday=True)
        with self._init_lock:
            init_status = self._init_status
            init_error = self._init_error
            init_started_at = self._init_started_at
            init_finished_at = self._init_finished_at
            init_result = dict(self._init_result)
        ready = repo_ready and layout['ok'] and init_status == 'ready'
        return {
            'status': 'ready' if ready else init_status,
            'service': 'vulture',
            'repo_ready': repo_ready,
            'dataset_ready': layout['ok'],
            'dataset_missing': layout['missing'],
            'init_status': init_status,
            'init_error': init_error,
            'init_started_at': init_started_at,
            'init_finished_at': init_finished_at,
            'init_result': init_result,
            'zip_inputs': {
                'signature_zip': str(DATA / 'signature.zip'),
                'aligned_patch_commits_zip': str(DATA / 'aligned_patch_commits.zip'),
                'result_zip': str(DATA / 'Result.zip'),
            },
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            'service': 'vulture',
            'input_modes': ['path'],
            'supported_input_kinds': ['directory', 'file'],
            'languages': ['c', 'cpp'],
            'stages': ['tpl_reuse', 'one_day_detection'],
            'outputs': ['tpl_reuse_raw', 'one_day_stdout', 'one_day_summary_json'],
            'async': True,
            'auto_extract': {
                'signature_zip': True,
                'aligned_patch_commits_zip': True,
                'result_zip': False,
            },
            'readiness_gate': {
                'enabled': True,
                'analyze_blocks_until_ready': True,
                'initialization_scope': 'repo_layout_and_dataset',
            },
        }

    def _log(self, message: str) -> None:
        print(f'[vulture-service] {message}', file=sys.stderr, flush=True)

    def start_initialization(self, run_oneday: bool = True) -> None:
        with self._init_lock:
            if self._init_status in {'initializing', 'ready'}:
                return
            self._init_status = 'initializing'
            self._init_error = None
            self._init_started_at = iso_now()
            self._init_finished_at = None
            self._init_result = {}
            self._init_run_oneday = run_oneday
            self._init_event.clear()
        thread = threading.Thread(target=self._initialize_dataset_thread, args=(run_oneday,), daemon=True)
        thread.start()

    def _initialize_dataset_thread(self, run_oneday: bool) -> None:
        try:
            self._log(f'initialization started run_oneday={run_oneday}')
            result = self._prepare_repo_layout(run_oneday=run_oneday)
            with self._init_lock:
                self._init_status = 'ready'
                self._init_error = None
                self._init_finished_at = iso_now()
                self._init_result = result
            self._log('initialization finished status=ready')
        except Exception as exc:
            with self._init_lock:
                self._init_status = 'failed'
                self._init_error = str(exc)
                self._init_finished_at = iso_now()
                self._init_result = {}
            self._log(f'initialization failed error={exc}')
        finally:
            self._init_event.set()

    def ensure_ready(self, run_oneday: bool, timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._init_lock:
            status = self._init_status
            should_start = status == 'new'
            if status == 'ready':
                return dict(self._init_result)
            if status == 'failed' and run_oneday and not self._init_run_oneday:
                should_start = True

        if should_start:
            self.start_initialization(run_oneday=run_oneday)

        completed = self._init_event.wait(timeout=timeout)
        if not completed:
            raise ServiceUnavailableError('dataset initialization is still running')

        with self._init_lock:
            status = self._init_status
            error = self._init_error
            result = dict(self._init_result)
        if status != 'ready':
            raise ServiceUnavailableError(f'dataset initialization failed: {error}')
        return result

    def submit_scan(self, req: Dict[str, Any]) -> Dict[str, Any]:
        target = Path(req['target_path'])
        if not target.is_absolute():
            raise ValueError('target_path must be an absolute path')
        if not target.exists():
            raise ValueError(f'target_path does not exist: {target}')
        kind = req.get('input_kind', 'directory')
        if kind == 'directory' and not target.is_dir():
            raise ValueError('input_kind=directory but target_path is not a directory')
        if kind == 'file' and not target.is_file():
            raise ValueError('input_kind=file but target_path is not a file')

        self.ensure_ready(run_oneday=bool(req.get('run_oneday_detection', True)), timeout=None)

        slug = req.get('job_name') or target.stem
        safe_slug = re.sub(r'[^A-Za-z0-9._-]+', '_', slug).strip('_') or 'job'
        job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_slug}_{uuid.uuid4().hex[:8]}"
        outdir = OUTPUT / job_id
        workdir = WORK / job_id
        ensure_dir(outdir)
        ensure_dir(workdir)

        job = Job(job_id=job_id, request=req, workdir=str(workdir), outdir=str(outdir))
        with self._lock:
            self._jobs[job_id] = job
        self._queue.put(job_id)
        return {'job_id': job_id, 'status': job.status}

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._job_dict(j) for j in self._jobs.values()]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._job_dict(job) if job else None

    def get_result(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return None
        result_path = Path(job.outdir) / 'result.json'
        if not result_path.exists():
            return None
        return json.loads(result_path.read_text(encoding='utf-8'))

    def get_artifacts(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return None
        outdir = Path(job.outdir)
        if not outdir.exists():
            return None
        artifacts = []
        for p in sorted(outdir.iterdir()):
            if p.is_file():
                artifacts.append({'name': p.name, 'path': str(p)})
        return {'job_id': job_id, 'artifacts': artifacts}

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            proc = self._procs.get(job_id)
            if not job:
                return False
            if job.status == 'queued':
                job.status = 'cancelled'
                job.finished_at = iso_now()
                return True
            if job.status != 'running' or proc is None:
                return False
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            job.status = 'cancelled'
            job.finished_at = iso_now()
            return True

    def _job_dict(self, job: Optional[Job]) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        return {
            'job_id': job.job_id,
            'status': job.status,
            'created_at': job.created_at,
            'started_at': job.started_at,
            'finished_at': job.finished_at,
            'error': job.error,
            'request': job.request,
        }

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
            if not job or job.status == 'cancelled':
                continue
            try:
                self._run_job(job)
            except Exception as e:
                with self._lock:
                    job.status = 'failed'
                    job.error = str(e)
                    job.finished_at = iso_now()
                    self._write_result(job, {'job_id': job.job_id, 'status': 'failed', 'error': str(e)})

    def _check_dataset_layout(self, run_oneday: bool) -> Dict[str, Any]:
        ok = True
        missing = []
        required = [
            DATA / 'signature' / 'osscollector',
            DATA / 'signature' / 'preprocessor',
        ]
        if run_oneday:
            required.extend([
                DATA / 'aligned_patch',
                DATA / 'aligned_cpe',
            ])
        for path in required:
            if not path.exists():
                ok = False
                missing.append(str(path))
        return {'ok': ok, 'missing': missing}

    def _validate_zip(self, zip_path: Path) -> None:
        if not zip_path.exists():
            raise RuntimeError(f'required zip file not found: {zip_path}')
        size = zip_path.stat().st_size
        self._log(f'checking zip path={zip_path} size={size}')
        if not zipfile.is_zipfile(zip_path):
            raise RuntimeError(f'file is not a valid zip archive: {zip_path}')
        with zipfile.ZipFile(zip_path, 'r') as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f'corrupt zip entry in {zip_path}: {bad}')
        self._log(f'zip check passed path={zip_path}')

    def _safe_extract_zip(self, zip_path: Path, tmp_dir: Path) -> None:
        root = tmp_dir.resolve()
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                target = (tmp_dir / info.filename).resolve()
                if target != root and not str(target).startswith(str(root) + os.sep):
                    raise RuntimeError(f'unsafe zip entry in {zip_path}: {info.filename}')
            zf.extractall(tmp_dir)

    def _extract_zip_if_needed(self, zip_path: Path, dest_dir: Path, expected_paths: List[Path]) -> bool:
        if all(p.exists() for p in expected_paths):
            self._log(f'dataset already extracted for zip={zip_path}')
            return False
        self._validate_zip(zip_path)
        ensure_dir(dest_dir)
        tmp_dir = dest_dir / f'.extract_tmp_{zip_path.stem}'
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        ensure_dir(tmp_dir)
        self._log(f'extracting zip={zip_path} dest={dest_dir}')
        self._safe_extract_zip(zip_path, tmp_dir)
        entries = [p for p in tmp_dir.iterdir() if p.name != '__MACOSX']
        source_root = entries[0] if len(entries) == 1 and entries[0].is_dir() else tmp_dir
        for child in list(source_root.iterdir()):
            target = dest_dir / child.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(child), str(target))
        shutil.rmtree(tmp_dir, ignore_errors=True)
        self._log(f'extraction finished zip={zip_path}')
        return True

    def _prepare_dataset(self, run_oneday: bool) -> Dict[str, Any]:
        extracted = {
            'signature': False,
            'aligned_patch_commits': False,
        }
        extracted['signature'] = self._extract_zip_if_needed(
            DATA / 'signature.zip',
            DATA / 'signature',
            [DATA / 'signature' / 'osscollector', DATA / 'signature' / 'preprocessor'],
        )
        if run_oneday:
            extracted['aligned_patch_commits'] = self._extract_zip_if_needed(
                DATA / 'aligned_patch_commits.zip',
                DATA,
                [DATA / 'aligned_patch', DATA / 'aligned_cpe'],
            )
        return extracted

    def _prepare_repo_layout(self, run_oneday: bool) -> Dict[str, Any]:
        if not VULTURE_REPO.exists():
            raise RuntimeError(f'Vulture repo not found: {VULTURE_REPO}. Run bootstrap_vulture.sh first.')
        self._log(f'preparing dataset and repository layout run_oneday={run_oneday}')
        extracted = self._prepare_dataset(run_oneday)
        layout = self._check_dataset_layout(run_oneday=run_oneday)
        if not layout['ok']:
            raise RuntimeError('Dataset layout incomplete: ' + ', '.join(layout['missing']))
        links = [
            (DATA / 'signature' / 'osscollector', VULTURE_REPO / 'TPLFilter' / 'src' / 'osscollector'),
            (DATA / 'signature' / 'preprocessor', VULTURE_REPO / 'TPLFilter' / 'src' / 'preprocessor'),
        ]
        if run_oneday:
            links.extend([
                (DATA / 'aligned_patch', VULTURE_REPO / 'OneDayDetector' / 'aligned_patch'),
                (DATA / 'aligned_cpe', VULTURE_REPO / 'OneDayDetector' / 'aligned_cpe'),
            ])
        for src, dst in links:
            if dst.is_symlink() or dst.exists():
                if dst.is_symlink() and dst.resolve() == src.resolve():
                    continue
                if dst.is_dir() and not dst.is_symlink() and any(dst.iterdir()):
                    continue
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                elif dst.is_dir():
                    shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.symlink_to(src, target_is_directory=True)
                self._log(f'linked {dst} -> {src}')
            except FileExistsError:
                pass
        self._log('repository layout ready')
        return extracted

    def _stage_input(self, job: Job) -> Path:
        target = Path(job.request['target_path'])
        staged_root = Path(job.workdir) / 'input'
        ensure_dir(staged_root)
        if job.request.get('input_kind') == 'file':
            dest_dir = staged_root / (job.request.get('job_name') or target.stem or 'input')
            ensure_dir(dest_dir)
            shutil.copy2(target, dest_dir / target.name)
            return dest_dir
        return target

    def _run_job(self, job: Job) -> None:
        req = job.request
        run_oneday = bool(req.get('run_oneday_detection', True))
        auto_extract = self.ensure_ready(run_oneday=run_oneday, timeout=None)
        job.status = 'running'
        job.started_at = iso_now()
        staged = self._stage_input(job)
        outdir = Path(job.outdir)
        stdout_log = outdir / 'stdout.log'
        stderr_log = outdir / 'stderr.log'
        raw_tpl = outdir / 'tpl_reuse_raw.txt'
        raw_fp = outdir / 'tpl_reuse_fp_eliminated.txt'
        raw_oneday = outdir / 'one_day_stdout.txt'

        timeout = int(req.get('timeout_seconds', 21600))
        project_name = Path(staged).name
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'

        tpl_results: Dict[str, Any] = {'stage_enabled': bool(req.get('run_tpl_reuse', True))}
        oneday_results: Dict[str, Any] = {'stage_enabled': run_oneday}

        with open(stdout_log, 'a', encoding='utf-8') as out, open(stderr_log, 'a', encoding='utf-8') as err:
            if req.get('run_tpl_reuse', True):
                proc = subprocess.Popen(
                    [VULTURE_PYTHON, 'Detector.py', str(staged)],
                    cwd=str(VULTURE_REPO / 'TPLReuseDetector'),
                    stdout=out,
                    stderr=err,
                    env=env,
                    preexec_fn=os.setsid,
                )
                with self._lock:
                    self._procs[job.job_id] = proc
                    job.pid = proc.pid
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    raise RuntimeError('TPL reuse detection timed out')
                if proc.returncode != 0:
                    raise RuntimeError(f'TPL reuse detection failed with exit code {proc.returncode}')

                result_file = VULTURE_REPO / 'TPLReuseDetector' / 'res' / f'result_{project_name}'
                if result_file.exists():
                    shutil.copy2(result_file, raw_tpl)
                    tpl_results['raw_lines'] = result_file.read_text(encoding='utf-8', errors='replace').splitlines()
                    tpl_results['parsed_lines'] = self._parse_tab_lines(tpl_results['raw_lines'])
                else:
                    tpl_results['raw_lines'] = []
                    tpl_results['parsed_lines'] = []

                func_result = VULTURE_REPO / 'TPLReuseDetector' / 'res' / f'result_{project_name}_func'
                if func_result.exists():
                    proc2 = subprocess.Popen(
                        [VULTURE_PYTHON, 'fp_eliminator.py', str(func_result)],
                        cwd=str(VULTURE_REPO / 'TPLReuseDetector'),
                        stdout=out,
                        stderr=err,
                        env=env,
                        preexec_fn=os.setsid,
                    )
                    with self._lock:
                        self._procs[job.job_id] = proc2
                        job.pid = proc2.pid
                    try:
                        proc2.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(proc2.pid), signal.SIGTERM)
                        raise RuntimeError('TPL false-positive elimination timed out')
                    modified_name = VULTURE_REPO / 'TPLReuseDetector' / f'modified_result_without_func{project_name}'
                    if modified_name.exists():
                        shutil.copy2(modified_name, raw_fp)
                        tpl_results['fp_eliminated_lines'] = modified_name.read_text(encoding='utf-8', errors='replace').splitlines()
                else:
                    tpl_results['fp_eliminated_lines'] = []

            if run_oneday:
                proc3 = subprocess.Popen(
                    [VULTURE_PYTHON, 'VersionBasedDetection.py', str(staged)],
                    cwd=str(VULTURE_REPO / 'OneDayDetector'),
                    stdout=subprocess.PIPE,
                    stderr=err,
                    env=env,
                    text=True,
                    preexec_fn=os.setsid,
                )
                with self._lock:
                    self._procs[job.job_id] = proc3
                    job.pid = proc3.pid
                try:
                    stdout_data, _ = proc3.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc3.pid), signal.SIGTERM)
                    raise RuntimeError('1-day detection timed out')
                raw_oneday.write_text(stdout_data, encoding='utf-8')
                out.write(stdout_data)
                if proc3.returncode != 0:
                    raise RuntimeError(f'1-day detection failed with exit code {proc3.returncode}')
                oneday_results['summary'] = self._parse_oneday_stdout(stdout_data)
                oneday_results['stdout'] = stdout_data.splitlines()

        with self._lock:
            self._procs.pop(job.job_id, None)
        result = {
            'job_id': job.job_id,
            'status': 'done',
            'service': 'vulture',
            'target_path': job.request['target_path'],
            'input_kind': job.request.get('input_kind', 'directory'),
            'dataset_auto_extracted': auto_extract,
            'tpl_reuse': tpl_results,
            'one_day_detection': oneday_results,
            'artifacts': self.get_artifacts(job.job_id)['artifacts'],
        }
        self._write_result(job, result)
        job.status = 'done'
        job.finished_at = iso_now()
        if not req.get('keep_workdir', False):
            shutil.rmtree(job.workdir, ignore_errors=True)

    def _write_result(self, job: Job, result: Dict[str, Any]) -> None:
        result_path = Path(job.outdir) / 'result.json'
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        meta_path = Path(job.outdir) / 'meta.json'
        meta_path.write_text(json.dumps(self._job_dict(job), ensure_ascii=False, indent=2), encoding='utf-8')
        job.result_path = str(result_path)

    def _parse_tab_lines(self, lines: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for line in lines:
            parts = line.split('	')
            if len(parts) >= 7:
                out.append({
                    'target': parts[0],
                    'component': parts[1],
                    'predicted_version': parts[2],
                    'used': self._to_int(parts[3]),
                    'unused': self._to_int(parts[4]),
                    'modified': self._to_int(parts[5]),
                    'structure_changed': parts[6].strip().lower() == 'true',
                    'raw_line': line,
                })
            else:
                out.append({'raw_line': line})
        return out

    def _parse_oneday_stdout(self, text: str) -> Dict[str, Any]:
        summary: Dict[str, Any] = {}
        patterns = {
            'vulnerable_cves_exact': r'^Vulnerable CVEs Exact:\s*(.+)$',
            'vulnerable_cves_modified': r'^Vulnerable CVEs Modified:\s*(.+)$',
            'patched_cves_exact': r'^Patched CVEs Exact:\s*(.+)$',
            'patched_cves_modified': r'^Patched CVEs Modified:\s*(.+)$',
            'version_detection': r'^Version Detection:\s*(.+)$',
        }
        for key, pat in patterns.items():
            m = re.search(pat, text, re.MULTILINE)
            if m:
                summary[key] = self._parse_pythonish_set(m.group(1).strip())
            else:
                summary[key] = []
        return summary

    def _parse_pythonish_set(self, value: str) -> List[str]:
        value = value.strip()
        if value.startswith('set(') and value.endswith(')'):
            inner = value[4:-1]
            if inner == '':
                return []
            if inner.startswith('{') and inner.endswith('}'):
                try:
                    data = ast.literal_eval(inner)
                    return sorted(str(x) for x in data)
                except Exception:
                    return [inner]
            return [inner]
        if value.startswith('{') and value.endswith('}'):
            try:
                data = ast.literal_eval(value)
                return sorted(str(x) for x in data)
            except Exception:
                return [value]
        return [value] if value else []

    def _to_int(self, s: str) -> Optional[int]:
        try:
            return int(s)
        except Exception:
            return None
