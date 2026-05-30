from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from worker import ServiceUnavailableError, VultureService

app = FastAPI(title='vulture_service', version='1.1.0')
svc = VultureService()


class ScanRequest(BaseModel):
    target_path: str
    job_name: Optional[str] = None
    input_kind: Literal['directory', 'file'] = 'directory'
    run_tpl_reuse: bool = True
    run_oneday_detection: bool = True
    timeout_seconds: int = Field(default=21600, ge=60, le=172800)
    keep_workdir: bool = False


@app.on_event('startup')
def startup() -> None:
    svc.start_initialization(run_oneday=True)


@app.get('/healthz')
def healthz():
    return svc.healthz()


@app.get('/readyz')
def readyz():
    status = svc.healthz()
    if not status.get('repo_ready') or not status.get('dataset_ready') or status.get('init_status') != 'ready':
        raise HTTPException(status_code=503, detail=status)
    return status


@app.get('/capabilities')
def capabilities():
    return svc.capabilities()


def submit(req: ScanRequest):
    try:
        return svc.submit_scan(req.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f'missing required field: {exc.args[0]}')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def submit_upload(
    *,
    file: UploadFile,
    job_name: Optional[str],
    input_kind: Literal['archive', 'file'],
    run_tpl_reuse: bool,
    run_oneday_detection: bool,
    timeout_seconds: int,
    keep_workdir: bool,
):
    if timeout_seconds < 60 or timeout_seconds > 172800:
        raise HTTPException(status_code=422, detail='timeout_seconds must be between 60 and 172800')

    request = {
        'job_name': job_name,
        'input_kind': input_kind,
        'run_tpl_reuse': run_tpl_reuse,
        'run_oneday_detection': run_oneday_detection,
        'timeout_seconds': timeout_seconds,
        'keep_workdir': keep_workdir,
    }

    try:
        reservation = svc.reserve_upload_scan(request, file.filename or 'upload.bin')
        size = 0
        with open(reservation['upload_path'], 'wb') as fp:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                fp.write(chunk)
                size += len(chunk)
        return svc.finish_upload_scan(reservation['job_id'], size)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f'missing required field: {exc.args[0]}')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        job_id = None
        if 'reservation' in locals() and isinstance(reservation, dict):
            job_id = reservation.get('job_id')
        if isinstance(job_id, str):
            svc.fail_upload_scan(job_id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        await file.close()


@app.post('/scan')
def scan(req: ScanRequest):
    return submit(req)


@app.post('/api/v1/analyze')
def api_analyze(req: ScanRequest):
    return submit(req)


@app.post('/scan/upload')
async def scan_upload(
    file: UploadFile = File(...),
    job_name: Optional[str] = Form(default=None),
    input_kind: Literal['archive', 'file'] = Form(default='archive'),
    run_tpl_reuse: bool = Form(default=True),
    run_oneday_detection: bool = Form(default=True),
    timeout_seconds: int = Form(default=21600),
    keep_workdir: bool = Form(default=False),
):
    return await submit_upload(
        file=file,
        job_name=job_name,
        input_kind=input_kind,
        run_tpl_reuse=run_tpl_reuse,
        run_oneday_detection=run_oneday_detection,
        timeout_seconds=timeout_seconds,
        keep_workdir=keep_workdir,
    )


@app.post('/api/v1/analyze/upload')
async def api_analyze_upload(
    file: UploadFile = File(...),
    job_name: Optional[str] = Form(default=None),
    input_kind: Literal['archive', 'file'] = Form(default='archive'),
    run_tpl_reuse: bool = Form(default=True),
    run_oneday_detection: bool = Form(default=True),
    timeout_seconds: int = Form(default=21600),
    keep_workdir: bool = Form(default=False),
):
    return await scan_upload(
        file=file,
        job_name=job_name,
        input_kind=input_kind,
        run_tpl_reuse=run_tpl_reuse,
        run_oneday_detection=run_oneday_detection,
        timeout_seconds=timeout_seconds,
        keep_workdir=keep_workdir,
    )


@app.get('/jobs')
def list_jobs():
    return {'jobs': svc.list_jobs()}


@app.get('/api/v1/jobs')
def api_list_jobs():
    return list_jobs()


@app.get('/jobs/{job_id}')
def get_job(job_id: str):
    job = svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='job not found')
    return job


@app.get('/api/v1/jobs/{job_id}')
def api_get_job(job_id: str):
    return get_job(job_id)


@app.get('/jobs/{job_id}/result')
def get_result(job_id: str):
    result = svc.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail='result not found')
    return result


@app.get('/api/v1/jobs/{job_id}/result')
def api_get_result(job_id: str):
    return get_result(job_id)


@app.get('/jobs/{job_id}/artifacts')
def get_artifacts(job_id: str):
    artifacts = svc.get_artifacts(job_id)
    if artifacts is None:
        raise HTTPException(status_code=404, detail='artifacts not found')
    return artifacts


@app.get('/api/v1/jobs/{job_id}/artifacts')
def api_get_artifacts(job_id: str):
    return get_artifacts(job_id)


@app.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    ok = svc.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail='job not found or not cancellable')
    return svc.get_job(job_id)


@app.post('/api/v1/jobs/{job_id}/cancel')
def api_cancel_job(job_id: str):
    return cancel_job(job_id)


@app.get('/api/v1/health')
def api_health():
    return healthz()


@app.get('/api/v1/capabilities')
def api_capabilities():
    return capabilities()
