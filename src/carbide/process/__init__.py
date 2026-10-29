"""Managed background-process control plane."""

from carbide.process.domain import BackgroundJob, JobStatus
from carbide.process.errors import (
    BackgroundJobError,
    JobNotFoundError,
    JobOwnershipError,
    JobStoreError,
)
from carbide.process.manager import (
    BackgroundJobManager,
    extract_background_pid,
    extract_log_path,
)
from carbide.process.repository import JobRepository

__all__ = [
    "BackgroundJob",
    "BackgroundJobError",
    "BackgroundJobManager",
    "JobNotFoundError",
    "JobOwnershipError",
    "JobRepository",
    "JobStatus",
    "JobStoreError",
    "extract_background_pid",
    "extract_log_path",
]
