from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobLogLinks(BaseModel):
    stdout_stderr: str
    lifecycle: str
    metadata: str


class ArtifactEntry(BaseModel):
    name: str
    url: str


class JobSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    job_id: str | None = Field(default=None, description='Optional caller-supplied job identifier.')


class JobSubmissionForm(BaseModel):
    files: list[str] = Field(description='Repeated multipart file parts submitted under the form field name "files".')
    job_id: str | None = Field(default=None, description='Optional caller-supplied job identifier.')


class JobResponse(BaseModel):
    job_id: str
    status: str | None = None
    container_id: str | None = None
    container_name: str | None = None
    job_dir: str | None = None
    data_dir: str | None = None
    kill_switch_path: str | None = None
    lifecycle_log_path: str | None = None
    log_path: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    error_message: str | None = None
    job_url: str
    status_url: str
    artifacts_url: str
    data_dir_url: str
    logs: JobLogLinks | None = None


class JobDetailResponse(JobResponse):
    artifacts: list[ArtifactEntry] = []


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    exit_code: int | None = None
    error_message: str | None = None
    artifacts_url: str


class JobArtifactsResponse(BaseModel):
    job_id: str
    data_dir: str
    files: list[ArtifactEntry]