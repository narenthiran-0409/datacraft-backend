"""Pure approval_requests.status recomputation logic — no DB access,
unit-testable in isolation. ApprovalService.decide() calls this after
inserting a decision, under the row lock (locked design item 4)."""


def recompute_approval_request_status(all_decisions: list[str], remaining_after: set) -> str:
    """all_decisions: every decision value ('APPROVE'/'REJECT') ever
    recorded under this approval_requests row, including the one just
    inserted. remaining_after: the resolved-issue scope still undecided
    after this decision.

    Mixed-outcome policy (locked design): if the remaining scope is empty
    and EVERY decision ever recorded was APPROVE -> APPROVED; if remaining
    is empty and at least one REJECT exists among all decisions ->
    REJECTED (even if other issues in the same request were approved).
    Otherwise -> PARTIALLY_APPROVED."""
    if not remaining_after:
        return "APPROVED" if all(d == "APPROVE" for d in all_decisions) else "REJECTED"
    return "PARTIALLY_APPROVED"


def resolved_scope_from_rows(rows: list[tuple]) -> tuple[list, int]:
    """rows: (issue_id, record_ref) pairs for every issue meeting the
    resolved-issue definition (locked design item 1). Returns
    (issue_ids, distinct_record_count) — affected_record_count counts
    unique underlying records, since more than one issue (e.g. two
    different rule failures) can point at the same record_ref."""
    issue_ids = [row[0] for row in rows]
    record_count = len({row[1] for row in rows})
    return issue_ids, record_count
