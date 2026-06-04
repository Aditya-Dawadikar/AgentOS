from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import uuid4
import shutil

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app_logging import get_logger
from db import get_job, list_jobs
from api.models import ArtifactEntry, JobArtifactsResponse, JobDetailResponse, JobResponse, JobStatusResponse, JobSubmissionForm, JobSubmissionRequest, JobLogLinks
from core.runner import data_root, jobs_root, run_job


router = APIRouter(prefix='/jobs', tags=['jobs'])
logger = get_logger(__name__)


def _safe_job_path(job_id: str) -> Path:
    return jobs_root / job_id


def _safe_relative_upload_path(filename: str) -> Path:
    normalized = filename.replace('\\', '/').strip('/')
    relative_path = Path(normalized)
    if not normalized or relative_path.is_absolute() or '..' in relative_path.parts:
        raise HTTPException(status_code=400, detail=f'Invalid upload path: {filename}')
    return relative_path


def _job_response(job: dict[str, object]) -> JobResponse:
    data_dir = job.get('data_dir')
    job_id = job['job_id']
    response = dict(job)
    response['job_url'] = f'/jobs/{job_id}'
    response['status_url'] = f'/jobs/{job_id}/status'
    response['artifacts_url'] = f'/jobs/{job_id}/artifacts'
    response['data_dir_url'] = f'/artifacts/{job_id}/'
    if data_dir:
        response['logs'] = JobLogLinks(
            stdout_stderr=f'/artifacts/{job_id}/container_logs.txt',
            lifecycle=f'/artifacts/{job_id}/container_lifecycle.txt',
            metadata=f'/artifacts/{job_id}/container_desc.txt',
        )
    return JobResponse.model_validate(response)


def _artifact_entries(job_id: str) -> list[ArtifactEntry]:
    artifact_root = data_root / job_id
    if not artifact_root.is_dir():
        return []

    entries: list[ArtifactEntry] = []
    for artifact_path in sorted(path for path in artifact_root.rglob('*') if path.is_file()):
        relative_path = artifact_path.relative_to(artifact_root).as_posix()
        entries.append(ArtifactEntry(name=relative_path,
                                     url=f'/artifacts/{job_id}/{relative_path}'))
    return entries


@router.post('', status_code=202, response_model=JobResponse)
async def submit_job(job_id: Annotated[str | None, Form()] = None,
                     files: list[UploadFile] = File(...)) -> JobResponse:
    if not files:
        logger.warning('job_submission_rejected reason=no_files')
        raise HTTPException(status_code=400, detail='At least one file must be uploaded')

    JobSubmissionForm(files=[upload.filename or '' for upload in files], job_id=job_id)
    request = JobSubmissionRequest(job_id=job_id)
    job_id = request.job_id or uuid4().hex
    job_dir = _safe_job_path(job_id)
    logger.info('job_submission_received job_id=%s file_count=%s', job_id, len(files))
    job_dir.mkdir(parents=True, exist_ok=False)

    try:
        for upload in files:
            relative_path = _safe_relative_upload_path(upload.filename or '')
            destination = job_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)

            with destination.open('wb') as output_file:
                shutil.copyfileobj(upload.file, output_file)

        if not (job_dir / 'job.py').is_file():
            logger.warning('job_submission_rejected job_id=%s reason=missing_job_py', job_id)
            raise HTTPException(status_code=400, detail='Submitted job must include job.py')

        response = _job_response(run_job(job_id))
        logger.info('job_submission_accepted job_id=%s container_id=%s',
                    response.job_id,
                    response.container_id)
        return response
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception('job_submission_failed job_id=%s error=%s', job_id, error)
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        for upload in files:
            await upload.close()


@router.get('', response_model=list[JobResponse])
def get_jobs() -> list[JobResponse]:
    jobs = [_job_response(job) for job in list_jobs()]
    logger.info('jobs_listed count=%s', len(jobs))
    return jobs


@router.get('/{job_id}', response_model=JobDetailResponse)
def get_job_details(job_id: str) -> JobDetailResponse:
    job = get_job(job_id)
    if job is None:
        logger.warning('job_lookup_failed job_id=%s endpoint=details', job_id)
        raise HTTPException(status_code=404, detail='Job not found')

    response = _job_response(job).model_dump()
    response['artifacts'] = _artifact_entries(job_id)
    logger.info('job_details_returned job_id=%s artifact_count=%s', job_id, len(response['artifacts']))
    return JobDetailResponse.model_validate(response)


@router.get('/{job_id}/status', response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    job = get_job(job_id)
    if job is None:
        logger.warning('job_lookup_failed job_id=%s endpoint=status', job_id)
        raise HTTPException(status_code=404, detail='Job not found')

    logger.info('job_status_returned job_id=%s status=%s', job_id, job['status'])
    return JobStatusResponse(job_id=job_id,
                             status=job['status'],
                             exit_code=job.get('exit_code'),
                             error_message=job.get('error_message'),
                             artifacts_url=f'/jobs/{job_id}/artifacts')


@router.get('/{job_id}/artifacts', response_model=JobArtifactsResponse)
def list_job_artifacts(job_id: str) -> JobArtifactsResponse:
    job = get_job(job_id)
    if job is None:
        logger.warning('job_lookup_failed job_id=%s endpoint=artifacts', job_id)
        raise HTTPException(status_code=404, detail='Job not found')

    files = _artifact_entries(job_id)
    logger.info('job_artifacts_listed job_id=%s file_count=%s', job_id, len(files))
    return JobArtifactsResponse(job_id=job_id,
                                data_dir=str(data_root / job_id),
                                files=files)