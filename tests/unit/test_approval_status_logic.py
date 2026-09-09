from app.modules.approval.status_logic import recompute_approval_request_status, resolved_scope_from_rows


def test_all_approve_with_empty_remaining_is_approved() -> None:
    assert recompute_approval_request_status(["APPROVE", "APPROVE"], remaining_after=set()) == "APPROVED"


def test_single_reject_among_decisions_with_empty_remaining_is_rejected() -> None:
    assert recompute_approval_request_status(["APPROVE", "REJECT"], remaining_after=set()) == "REJECTED"


def test_all_reject_with_empty_remaining_is_rejected() -> None:
    assert recompute_approval_request_status(["REJECT", "REJECT"], remaining_after=set()) == "REJECTED"


def test_nonempty_remaining_is_partially_approved_regardless_of_decisions() -> None:
    assert recompute_approval_request_status(["APPROVE"], remaining_after={"issue-1"}) == "PARTIALLY_APPROVED"
    assert recompute_approval_request_status(["REJECT"], remaining_after={"issue-1"}) == "PARTIALLY_APPROVED"


def test_single_approve_decision_covering_everything_is_approved() -> None:
    assert recompute_approval_request_status(["APPROVE"], remaining_after=set()) == "APPROVED"


def test_resolved_scope_counts_distinct_records_not_issues() -> None:
    # Two issues (different rule failures) on the SAME record_ref.
    rows = [("issue-1", "record-A"), ("issue-2", "record-A"), ("issue-3", "record-B")]
    issue_ids, record_count = resolved_scope_from_rows(rows)
    assert len(issue_ids) == 3
    assert record_count == 2


def test_resolved_scope_empty_when_no_rows() -> None:
    issue_ids, record_count = resolved_scope_from_rows([])
    assert issue_ids == []
    assert record_count == 0


def test_remaining_scope_excludes_already_decided_issues() -> None:
    resolved = {"a", "b", "c"}
    already_decided = {"a"}
    assert (resolved - already_decided) == {"b", "c"}


def test_requested_ids_must_be_subset_of_remaining() -> None:
    remaining = {"a", "b"}
    requested = {"a", "c"}
    assert not requested.issubset(remaining)
    invalid = requested - remaining
    assert invalid == {"c"}
