from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Literal, Optional
from worker import VultureService

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

@app.get('/healthz')
def healthz():
    return svc.healthz()

@app.get('/capabilities')
def capabilities():
    return svc.capabilities()

@app.post('/scan')
def scan(req: ScanRequest):
    return svc.submit_scan(req.model_dump())

@app.get('/jobs')
def list_jobs():
    return {'jobs': svc.list_jobs()}

@app.get('/jobs/{job_id}')
def get_job(job_id: str):
    job = svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail='job not found')
    return job

@app.get('/jobs/{job_id}/result')
def get_result(job_id: str):
    result = svc.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail='result not found')
    return result

@app.get('/jobs/{job_id}/artifacts')
def get_artifacts(job_id: str):
    artifacts = svc.get_artifacts(job_id)
    if artifacts is None:
        raise HTTPException(status_code=404, detail='artifacts not found')
    return artifacts

@app.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    ok = svc.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail='job not found or not cancellable')
    return svc.get_job(job_id)
