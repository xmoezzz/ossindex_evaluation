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
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
WORK = ROOT / 'work'
OUTPUT = ROOT / 'output'
VENDOR = ROOT / 'vendor'
VULTURE_REPO = VENDOR / 'Vulture'
VULTURE_PYTHON = os.environ.get('VULTURE_PYTHON', 'python3.11')


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
            'dataset_checked': layout['checked'],
            'runtime_layout': 'official_vendor_layout',
            'runtime_data_paths': {
                'tpl_osscollector': str(VULTURE_REPO / 'TPLFilter' / 'src' / 'osscollector'),
                'tpl_preprocessor': str(VULTURE_REPO / 'TPLFilter' / 'src' / 'preprocessor'),
                'oneday_aligned_patch': str(VULTURE_REPO / 'OneDayDetector' / 'aligned_patch'),
                'oneday_aligned_cpe': str(VULTURE_REPO / 'OneDayDetector' / 'aligned_cpe'),
            },
            'init_status': init_status,
            'init_error': init_error,
            'init_started_at': init_started_at,
            'init_finished_at': init_finished_at,
            'init_result': init_result,
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            'service': 'vulture',
            'input_modes': ['upload', 'path'],
            'supported_input_kinds': ['archive', 'directory', 'file'],
            'languages': ['c', 'cpp'],
            'stages': ['tpl_reuse', 'one_day_detection'],
            'outputs': ['tpl_reuse_raw', 'one_day_stdout', 'one_day_summary_json'],
            'async': True,
            'runtime_layout': 'official_vendor_layout',
            'auto_extract': {
                'signature_zip': False,
                'aligned_patch_commits_zip': False,
                'result_zip': False,
            },
            'readiness_gate': {
                'enabled': True,
                'analyze_blocks_until_ready': True,
                'initialization_scope': 'official_vendor_runtime_layout',
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

    def _create_job(self, req: Dict[str, Any], slug_source: str) -> Job:
        slug = req.get('job_name') or slug_source
        safe_slug = re.sub(r'[^A-Za-z0-9._-]+', '_', str(slug)).strip('_') or 'job'
        job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_slug}_{uuid.uuid4().hex[:8]}"
        outdir = OUTPUT / job_id
        workdir = WORK / job_id
        ensure_dir(outdir)
        ensure_dir(workdir)
        job = Job(job_id=job_id, request=req, workdir=str(workdir), outdir=str(outdir))
        with self._lock:
            self._jobs[job_id] = job
        return job

    def reserve_upload_scan(self, req: Dict[str, Any], original_filename: str) -> Dict[str, Any]:
        kind = req.get('input_kind', 'archive')
        if kind not in {'archive', 'file'}:
            raise ValueError('input_kind must be archive or file for upload scans')

        self.ensure_ready(run_oneday=bool(req.get('run_oneday_detection', True)), timeout=None)

        upload_name = self._safe_upload_filename(original_filename)
        request = dict(req)
        request['input_mode'] = 'upload'
        request['upload_filename'] = upload_name
        request.pop('target_path', None)
        job = self._create_job(request, Path(upload_name).stem or 'upload')
        job.status = 'receiving'
        upload_dir = Path(job.workdir) / 'upload'
        ensure_dir(upload_dir)
        upload_path = upload_dir / upload_name
        job.request['uploaded_path'] = str(upload_path)
        self._write_result(job, {
            'job_id': job.job_id,
            'status': 'receiving',
            'service': 'vulture',
            'input_mode': 'upload',
            'upload_filename': upload_name,
        })
        return {'job_id': job.job_id, 'status': job.status, 'upload_path': str(upload_path)}

    def finish_upload_scan(self, job_id: str, size: int) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError('job not found')
            if job.status != 'receiving':
                raise ValueError(f'job is not receiving upload: {job.status}')
            upload_path = Path(job.request['uploaded_path'])
            if not upload_path.is_file():
                raise ValueError(f'uploaded file does not exist: {upload_path}')
            job.request['uploaded_size'] = size
            job.status = 'queued'
            self._write_result(job, {
                'job_id': job.job_id,
                'status': 'queued',
                'service': 'vulture',
                'input_mode': 'upload',
                'upload_filename': job.request.get('upload_filename'),
                'uploaded_size': size,
            })
        self._queue.put(job_id)
        return {'job_id': job_id, 'status': 'queued'}

    def fail_upload_scan(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = 'failed'
            job.error = error
            job.finished_at = iso_now()
            self._write_result(job, {'job_id': job.job_id, 'status': 'failed', 'error': error})

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

        request = dict(req)
        request['input_mode'] = 'path'
        job = self._create_job(request, target.stem)
        self._queue.put(job.job_id)
        return {'job_id': job.job_id, 'status': job.status}

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
        required = [
            VULTURE_REPO / 'TPLFilter' / 'src' / 'osscollector',
            VULTURE_REPO / 'TPLFilter' / 'src' / 'preprocessor',
        ]
        if run_oneday:
            required.extend([
                VULTURE_REPO / 'OneDayDetector' / 'aligned_patch',
                VULTURE_REPO / 'OneDayDetector' / 'aligned_cpe',
            ])
        missing = [str(path) for path in required if not path.is_dir()]
        return {
            'ok': len(missing) == 0,
            'missing': missing,
            'checked': [str(path) for path in required],
        }

    def _safe_extract_zip(self, zip_path: Path, tmp_dir: Path) -> None:
        root = tmp_dir.resolve()
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                target = (tmp_dir / info.filename).resolve()
                if target != root and not str(target).startswith(str(root) + os.sep):
                    raise RuntimeError(f'unsafe zip entry in {zip_path}: {info.filename}')
            zf.extractall(tmp_dir)

    def _prepare_repo_layout(self, run_oneday: bool) -> Dict[str, Any]:
        if not VULTURE_REPO.exists():
            raise RuntimeError(f'Vulture repo not found: {VULTURE_REPO}. Run bootstrap_vulture.sh first.')
        self._log(f'checking official VULTURE runtime layout run_oneday={run_oneday}')
        layout = self._check_dataset_layout(run_oneday=run_oneday)
        if not layout['ok']:
            raise RuntimeError('Official VULTURE runtime data layout incomplete: ' + ', '.join(layout['missing']))
        self._log('official VULTURE runtime layout ready')
        return {
            'official_vendor_layout': True,
            'checked_paths': layout['checked'],
        }

    def _safe_vulture_repo_name(self, job: Job, target: Path) -> str:
        raw = str(job.request.get('job_name') or target.stem or 'repo')
        raw = raw.replace('_', '-')
        safe = re.sub(r'[^A-Za-z0-9.-]+', '-', raw).strip('.-')
        return safe or 'repo'

    def _safe_upload_filename(self, value: str) -> str:
        raw = Path(value or 'upload.bin').name
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', raw).strip('._')
        return safe or 'upload.bin'

    def _safe_extract_tar_upload(self, archive_path: Path, dest_dir: Path) -> None:
        root = dest_dir.resolve()
        with tarfile.open(archive_path, 'r:*') as tf:
            for member in tf.getmembers():
                target = (dest_dir / member.name).resolve()
                if target != root and not str(target).startswith(str(root) + os.sep):
                    raise RuntimeError(f'unsafe tar entry in upload archive: {member.name}')
            tf.extractall(dest_dir)

    def _safe_extract_upload_archive(self, archive_path: Path, dest_dir: Path) -> str:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        ensure_dir(dest_dir)
        if zipfile.is_zipfile(archive_path):
            self._safe_extract_zip(archive_path, dest_dir)
            return 'zip'
        if tarfile.is_tarfile(archive_path):
            self._safe_extract_tar_upload(archive_path, dest_dir)
            return 'tar'
        raise ValueError(f'unsupported upload archive type: {archive_path.name}')

    def _oneday_canonical_key_candidates(self, component: str) -> List[str]:
        aligned_patch = VULTURE_REPO / 'OneDayDetector' / 'aligned_patch'
        aligned_cpe = VULTURE_REPO / 'OneDayDetector' / 'aligned_cpe'
        if not aligned_patch.is_dir():
            raise RuntimeError(f'OneDayDetector aligned_patch directory is missing: {aligned_patch}')
        if not aligned_cpe.is_dir():
            raise RuntimeError(f'OneDayDetector aligned_cpe directory is missing: {aligned_cpe}')

        patch_keys = {
            path.name
            for path in aligned_patch.iterdir()
            if path.is_dir() and '_' in path.name
        }
        cpe_keys = {
            path.name[:-5]
            for path in aligned_cpe.iterdir()
            if path.is_file() and path.name.endswith('.json') and '_' in path.name[:-5]
        }
        keys = sorted(patch_keys & cpe_keys)
        candidates: List[str] = []
        for key in keys:
            owner, repo = key.split('_', 1)
            if owner and repo == component:
                candidates.append(key)
        return candidates

    def _canonicalize_oneday_component(self, component: str) -> str:
        if '@@' in component:
            owner, repo = component.split('@@', 1)
            if not owner or not repo:
                raise RuntimeError(f'invalid OneDayDetector component key: {component}')
            return component

        candidates = self._oneday_canonical_key_candidates(component)
        if len(candidates) != 1:
            raise RuntimeError(
                'cannot canonicalize OneDayDetector component key '
                f'{component!r}: expected exactly one aligned_patch/aligned_cpe match, got {candidates}'
            )
        owner, repo = candidates[0].split('_', 1)
        return f'{owner}@@{repo}'

    def _normalize_oneday_reuse_info_file(self, path: Path) -> List[str]:
        if not path.exists():
            raise RuntimeError(f'OneDayDetector reuse-info file does not exist: {path}')

        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        normalized: List[str] = []
        changed = False
        for line_no, line in enumerate(lines, start=1):
            if not line.strip() or line.startswith('\t'):
                normalized.append(line)
                continue

            match = re.match(r'^(\S+)\s+(\S+)\s*:\s*$', line)
            if match is None:
                raise RuntimeError(f'invalid OneDayDetector reuse-info line: {path}:{line_no}: {line}')

            component = match.group(1)
            version = match.group(2)
            canonical_component = self._canonicalize_oneday_component(component)
            if canonical_component != component:
                changed = True
            normalized.append(f'{canonical_component} {version} :')

        if changed:
            path.write_text('\n'.join(normalized) + ('\n' if normalized else ''), encoding='utf-8')
        return normalized

    def _copy_tree(self, src: Path, dst: Path) -> None:
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)
        for path in sorted(src.rglob('*'), key=lambda p: str(p)):
            if path.is_symlink():
                continue
            rel = path.relative_to(src)
            target = dst / rel
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

    def _stage_input(self, job: Job) -> Path:
        staged_root = Path(job.workdir) / 'input'
        ensure_dir(staged_root)

        if job.request.get('input_mode') == 'upload':
            upload_path = Path(job.request['uploaded_path'])
            if not upload_path.is_file():
                raise ValueError(f'uploaded file does not exist: {upload_path}')
            repo_name = self._safe_vulture_repo_name(job, upload_path)
            dest_dir = staged_root / repo_name
            kind = job.request.get('input_kind', 'archive')
            if kind == 'file':
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                ensure_dir(dest_dir)
                shutil.copy2(upload_path, dest_dir / upload_path.name)
                self._log(f'staged uploaded file source={upload_path} dest={dest_dir} repo_name={repo_name}')
            elif kind == 'archive':
                archive_kind = self._safe_extract_upload_archive(upload_path, dest_dir)
                self._log(
                    f'staged uploaded archive source={upload_path} dest={dest_dir} '
                    f'repo_name={repo_name} archive_kind={archive_kind}'
                )
            else:
                raise ValueError('input_kind must be archive or file for upload scans')
            return dest_dir

        target = Path(job.request['target_path'])
        repo_name = self._safe_vulture_repo_name(job, target)
        dest_dir = staged_root / repo_name
        if job.request.get('input_kind') == 'file':
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            ensure_dir(dest_dir)
            shutil.copy2(target, dest_dir / target.name)
        else:
            self._copy_tree(target, dest_dir)
        self._log(f'staged input source={target} dest={dest_dir} repo_name={repo_name}')
        return dest_dir

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
                modified_name = VULTURE_REPO / 'TPLReuseDetector' / f'modified_result_without_func{project_name}'
                if func_result.exists():
                    func_text = func_result.read_text(encoding='utf-8', errors='replace').strip()
                    try:
                        func_json = json.loads(func_text) if func_text else {}
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f'invalid TPL function result JSON: {func_result}: {exc}') from exc

                    if not isinstance(func_json, dict):
                        raise RuntimeError(f'invalid TPL function result shape: {func_result}')

                    if not func_json:
                        modified_name.write_text('', encoding='utf-8')
                        shutil.copy2(modified_name, raw_fp)
                        tpl_results['fp_eliminated_lines'] = []
                    elif project_name not in func_json:
                        keys = sorted(str(k) for k in func_json.keys())[:20]
                        raise RuntimeError(
                            f'TPL function result repo mismatch: expected {project_name}, got keys={keys}'
                        )
                    else:
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
                        if proc2.returncode != 0:
                            raise RuntimeError(f'TPL false-positive elimination failed with exit code {proc2.returncode}')
                        if modified_name.exists():
                            tpl_results['fp_eliminated_lines'] = self._normalize_oneday_reuse_info_file(modified_name)
                            shutil.copy2(modified_name, raw_fp)
                        else:
                            raise RuntimeError(f'TPL false-positive elimination did not create expected output: {modified_name}')
                else:
                    modified_name.write_text('', encoding='utf-8')
                    shutil.copy2(modified_name, raw_fp)
                    tpl_results['fp_eliminated_lines'] = []

            if run_oneday:
                if req.get('run_tpl_reuse', True):
                    oneday_reuse_file = VULTURE_REPO / 'TPLReuseDetector' / f'modified_result_without_func{project_name}'
                    if oneday_reuse_file.exists():
                        tpl_results['fp_eliminated_lines'] = self._normalize_oneday_reuse_info_file(oneday_reuse_file)
                        shutil.copy2(oneday_reuse_file, raw_fp)
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
            'input_mode': job.request.get('input_mode', 'path'),
            'target_path': job.request.get('target_path'),
            'uploaded_filename': job.request.get('upload_filename'),
            'uploaded_size': job.request.get('uploaded_size'),
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
