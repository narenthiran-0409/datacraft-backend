"""Pure review_runs status-transition validators — no DB access, unit-
testable in isolation. ReviewService applies the actual mutation after
calling these."""
from app.core.exceptions import InvalidReviewStatusTransitionError

_ARCHIVE_ALLOWED_FROM = frozenset({"DRAFT", "IN_REVIEW", "READY_FOR_APPROVAL"})
_RESTORE_ALLOWED_FROM = frozenset({"ARCHIVED"})


def validate_archive_transition(current_status: str) -> None:
    if current_status not in _ARCHIVE_ALLOWED_FROM:
        raise InvalidReviewStatusTransitionError(f"Cannot archive a review run in status {current_status}")


def validate_restore_transition(current_status: str) -> None:
    if current_status not in _RESTORE_ALLOWED_FROM:
        raise InvalidReviewStatusTransitionError(f"Cannot restore a review run in status {current_status}")
