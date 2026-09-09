import pytest

from app.core.exceptions import InvalidReviewStatusTransitionError
from app.modules.review.state_machine import validate_archive_transition, validate_restore_transition


@pytest.mark.parametrize("status", ["DRAFT", "IN_REVIEW", "READY_FOR_APPROVAL"])
def test_archive_allowed_from_non_archived_statuses(status) -> None:
    validate_archive_transition(status)  # should not raise


def test_archive_rejected_when_already_archived() -> None:
    with pytest.raises(InvalidReviewStatusTransitionError):
        validate_archive_transition("ARCHIVED")


def test_restore_allowed_from_archived() -> None:
    validate_restore_transition("ARCHIVED")  # should not raise


@pytest.mark.parametrize("status", ["DRAFT", "IN_REVIEW", "READY_FOR_APPROVAL"])
def test_restore_rejected_from_non_archived_statuses(status) -> None:
    with pytest.raises(InvalidReviewStatusTransitionError):
        validate_restore_transition(status)
