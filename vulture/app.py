from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from worker import ServiceUnavailableError, VultureService

app = FastAPI(title='vulture_service', version='1.0.0')
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


@app.post('/scan')
def scan(req: ScanRequest):
    return submit(req)


@app.post('/api/v1/analyze')
def api_analyze(req: ScanRequest):
    return submit(req)


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
