"""Automation domain model: statuses, transitions, capabilities, typed errors."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

PLATFORMS = ("youtube", "facebook", "tiktok", "instagram")

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "facebook": "Facebook",
    "tiktok": "TikTok",
    "instagram": "Instagram",
}


class RunStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ACTION_REQUIRED = "ACTION_REQUIRED"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    AUTH_CHECK = "AUTH_CHECK"
    PREPARING = "PREPARING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    VERIFYING = "VERIFYING"
    PUBLISHED = "PUBLISHED"
    SCHEDULED = "SCHEDULED"
    PAUSED = "PAUSED"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Jobs the user may delete outright from the Queue. In-flight jobs must be
# cancelled first (their worker still holds runtime state), while PUBLISHED /
# FAILED / CANCELLED / SCHEDULED are finished records that only need cleanup.
DELETABLE_JOB_STATUSES = frozenset({
    JobStatus.PUBLISHED.value, JobStatus.FAILED.value,
    JobStatus.CANCELLED.value, JobStatus.SCHEDULED.value,
})


class AgentState(str, Enum):
    STARTING = "STARTING"
    AUTHENTICATED = "AUTHENTICATED"
    OBSERVING = "OBSERVING"
    REASONING = "REASONING"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    WAITING_USER = "WAITING_USER"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


_JOB_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.QUEUED.value: {JobStatus.AUTH_CHECK.value, JobStatus.SCHEDULED.value,
                             JobStatus.PAUSED.value, JobStatus.CANCELLED.value},
    JobStatus.SCHEDULED.value: {JobStatus.QUEUED.value, JobStatus.PAUSED.value,
                                JobStatus.CANCELLED.value},
    JobStatus.AUTH_CHECK.value: {JobStatus.PREPARING.value, JobStatus.RETRYING.value,
                                 JobStatus.FAILED.value, JobStatus.ACTION_REQUIRED.value,
                                 JobStatus.CANCELLED.value},
    JobStatus.PREPARING.value: {JobStatus.UPLOADING.value, JobStatus.RETRYING.value,
                                JobStatus.FAILED.value, JobStatus.ACTION_REQUIRED.value,
                                JobStatus.CANCELLED.value},
    JobStatus.UPLOADING.value: {JobStatus.PROCESSING.value, JobStatus.VERIFYING.value,
                                JobStatus.RETRYING.value, JobStatus.FAILED.value,
                                JobStatus.ACTION_REQUIRED.value, JobStatus.CANCELLED.value},
    JobStatus.PROCESSING.value: {JobStatus.VERIFYING.value, JobStatus.RETRYING.value,
                                 JobStatus.FAILED.value, JobStatus.ACTION_REQUIRED.value,
                                 JobStatus.CANCELLED.value},
    JobStatus.VERIFYING.value: {JobStatus.PUBLISHED.value, JobStatus.SCHEDULED.value,
                                JobStatus.RETRYING.value, JobStatus.FAILED.value,
                                JobStatus.ACTION_REQUIRED.value},
    JobStatus.RETRYING.value: {JobStatus.QUEUED.value, JobStatus.FAILED.value,
                               JobStatus.CANCELLED.value},
    JobStatus.FAILED.value: {JobStatus.QUEUED.value},          # manual retry
    JobStatus.ACTION_REQUIRED.value: {JobStatus.QUEUED.value, JobStatus.CANCELLED.value,
                                       JobStatus.PAUSED.value},
    JobStatus.PAUSED.value: {JobStatus.QUEUED.value, JobStatus.CANCELLED.value},
    JobStatus.PUBLISHED.value: set(),
    JobStatus.CANCELLED.value: set(),
}

_RUN_TRANSITIONS: dict[str, set[str]] = {
    RunStatus.DRAFT.value: {RunStatus.READY.value, RunStatus.QUEUED.value,
                            RunStatus.CANCELLED.value},
    RunStatus.READY.value: {RunStatus.QUEUED.value, RunStatus.CANCELLED.value},
    RunStatus.QUEUED.value: {RunStatus.RUNNING.value, RunStatus.CANCELLED.value},
    RunStatus.RUNNING.value: {RunStatus.PARTIAL_SUCCESS.value, RunStatus.COMPLETED.value,
                              RunStatus.FAILED.value, RunStatus.CANCELLED.value,
                              RunStatus.ACTION_REQUIRED.value, RunStatus.QUEUED.value},
    RunStatus.PARTIAL_SUCCESS.value: {RunStatus.QUEUED.value, RunStatus.RUNNING.value},
    RunStatus.ACTION_REQUIRED.value: {RunStatus.QUEUED.value, RunStatus.CANCELLED.value,
                                       RunStatus.RUNNING.value},
    RunStatus.FAILED.value: {RunStatus.QUEUED.value},
    RunStatus.COMPLETED.value: set(),
    RunStatus.CANCELLED.value: set(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in _JOB_TRANSITIONS.get(str(src), set()) or dst in _RUN_TRANSITIONS.get(str(src), set())


def job_can_transition(src: str, dst: str) -> bool:
    return dst == str(src) or dst in _JOB_TRANSITIONS.get(str(src), set())


def run_can_transition(src: str, dst: str) -> bool:
    return dst == str(src) or dst in _RUN_TRANSITIONS.get(str(src), set())


JOB_TERMINAL_SUCCESS = {JobStatus.PUBLISHED.value, JobStatus.SCHEDULED.value}
JOB_TERMINAL = JOB_TERMINAL_SUCCESS | {JobStatus.FAILED.value, JobStatus.CANCELLED.value}
JOB_ACTIVE = {JobStatus.QUEUED.value, JobStatus.SCHEDULED.value, JobStatus.AUTH_CHECK.value,
              JobStatus.PREPARING.value, JobStatus.UPLOADING.value, JobStatus.PROCESSING.value,
              JobStatus.VERIFYING.value, JobStatus.RETRYING.value}


def derive_run_status(job_statuses: list[str]) -> str:
    """Aggregate child job statuses into the run status (spec §13/§65)."""
    st = [str(s) for s in job_statuses]
    if not st:
        return RunStatus.DRAFT.value
    if all(s == JobStatus.CANCELLED.value for s in st):
        return RunStatus.CANCELLED.value
    if any(s in JOB_ACTIVE for s in st):
        if any(s in JOB_ACTIVE - {JobStatus.QUEUED.value, JobStatus.SCHEDULED.value} for s in st):
            return RunStatus.RUNNING.value
        return RunStatus.QUEUED.value
    if any(s == JobStatus.ACTION_REQUIRED.value for s in st):
        return RunStatus.ACTION_REQUIRED.value
    succeeded = sum(1 for s in st if s in JOB_TERMINAL_SUCCESS)
    if succeeded == len(st):
        return RunStatus.COMPLETED.value
    if succeeded > 0:
        return RunStatus.PARTIAL_SUCCESS.value
    if any(s == JobStatus.FAILED.value for s in st):
        return RunStatus.FAILED.value
    return RunStatus.CANCELLED.value


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw[:16]}"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# Typed error categories (spec §54)
# --------------------------------------------------------------------------- #

class AutomationError(RuntimeError):
    category = "UNKNOWN_RESULT"
    retryable = False
    user_message = ""
    # Machine-readable reason code for the Queue UI (spec §8/§9):
    # draft_ready / captcha / login_required / verify_unknown / policy_blocked.
    # Empty means "no specific reason" and is rendered as no chip.
    reason = ""

    def __init__(self, message: str = "", *, category: str | None = None,
                 user_message: str = "", retryable: bool | None = None,
                 reason: str = ""):
        super().__init__(message or self.__class__.__name__)
        if category:
            self.category = category
        if retryable is not None:
            self.retryable = retryable
        self.user_message = user_message or str(message)
        self.reason = str(reason or "")


class RetryableError(AutomationError):
    category = "NETWORK_ERROR"
    retryable = True


class AuthRequiredError(AutomationError):
    category = "AUTH_REQUIRED"


class PermissionDeniedError(AutomationError):
    category = "PERMISSION_DENIED"


class QuotaExceededError(AutomationError):
    category = "QUOTA_EXCEEDED"


class RateLimitedError(AutomationError):
    category = "RATE_LIMITED"
    retryable = True


class InvalidMediaError(AutomationError):
    category = "INVALID_MEDIA"


class NeedsActionError(AutomationError):
    category = "USER_ACTION_REQUIRED"


class VerificationFailedError(AutomationError):
    category = "VERIFICATION_FAILED"
    retryable = True


class DuplicateSuspectedError(AutomationError):
    category = "DUPLICATE_SUSPECTED"


class DuplicateInQueueError(AutomationError):
    """The requested upload is already waiting in the queue (spec §26).

    Raised by ``create_run`` *before* any row is written, so a repeat request for
    the same media+platform+account cannot silently add a duplicate Queue card.
    """
    category = "DUPLICATE_SUSPECTED"
    user_message = ("Video ini sudah ada di Queue (menunggu atau tertahan). "
                    "Buka tab Queue lalu lanjutkan atau hapus job itu dulu.")


class UnsupportedOperationError(AutomationError):
    category = "UNSUPPORTED_OPERATION"


class CancelledError(AutomationError):
    category = "CANCELLED"


class MetadataInvalidError(AutomationError):
    """Required publish metadata is missing or unusable (spec §8.4).

    Raised *before* a request is sent so a clip is never uploaded silently with
    an empty title/description (or an undeclared YouTube audience). The job is
    parked as ACTION_REQUIRED instead of being retried blindly.
    """
    category = "METADATA_INVALID"


# --------------------------------------------------------------------------- #
# Capability discovery (spec §49/§88)
# --------------------------------------------------------------------------- #

@dataclass
class PlatformCapabilities:
    platform: str
    can_publish_video: bool = True
    can_schedule: bool = False
    can_set_privacy: bool = True
    browser_fallback: bool = False
    auth_mode: str = "oauth"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "can_publish_video": self.can_publish_video,
            "can_schedule": self.can_schedule,
            "can_set_privacy": self.can_set_privacy,
            "browser_fallback": self.browser_fallback,
            "auth_mode": self.auth_mode,
        }


def capabilities_for(platform: str, auth_mode: str = "oauth") -> PlatformCapabilities:
    caps = PlatformCapabilities(platform=platform, auth_mode=auth_mode)
    if platform == "youtube":
        caps.can_schedule = True                       # publishAt (privacy=private)
        caps.browser_fallback = False
    elif platform == "facebook":
        caps.can_schedule = False                      # Graph API page video scheduling varies
        caps.browser_fallback = True                   # Browser Session mode (advanced)
        caps.auth_mode = auth_mode
    elif platform == "tiktok":
        caps.can_schedule = False                      # Direct Post schedules via TikTok UI/app only
        caps.browser_fallback = True                   # Browser Session mode (advanced)
        caps.auth_mode = auth_mode
    elif platform == "instagram":
        caps.can_schedule = False
        caps.browser_fallback = True                   # Browser Session mode
        caps.auth_mode = auth_mode
    return caps


def human_job_status(status: str) -> str:
    return {
        "QUEUED": "Queued",
        "AUTH_CHECK": "Auth check",
        "PREPARING": "Preparing",
        "UPLOADING": "Uploading",
        "PROCESSING": "Processing",
        "VERIFYING": "Verifying",
        "PUBLISHED": "Published",
        "SCHEDULED": "Scheduled",
        "PAUSED": "Paused",
        "ACTION_REQUIRED": "Action required",
        "RETRYING": "Retrying",
        "FAILED": "Failed",
        "CANCELLED": "Cancelled",
    }.get(status, status)
