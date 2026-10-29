"""Errors raised by the background-job control plane."""

from __future__ import annotations


class BackgroundJobError(RuntimeError):
    """Base class for recoverable background-job failures."""


class JobNotFoundError(BackgroundJobError):
    """The requested job metadata does not exist."""


class JobOwnershipError(BackgroundJobError):
    """A caller attempted to operate on another session's job."""


class JobStoreError(BackgroundJobError):
    """Job metadata could not be read or written safely."""
