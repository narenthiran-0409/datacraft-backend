"""One-time backfill: constructs lineage_records edges for all Phase 1-9
data that existed before Phase 10 instrumentation shipped.

Not run automatically by any migration or app startup path — run manually,
once, against the target database:

    python scripts/backfill_lineage.py

READ-ONLY with respect to every Phase 1-9 table (data_sources, connections,
schemas, datasets, columns, profile_runs, validation_runs, review_runs,
issues, corrections, approval_requests, approval_decisions,
approval_decision_issues, staging_runs, publish_runs) — the only writes
this script ever performs are INSERT ... ON CONFLICT DO NOTHING statements
against lineage_records, via LineageService.record_edge/record_edges_bulk
(the exact same idempotent mechanism the 9 live touch points use). Safe to
run more than once: re-running produces IDENTICAL row counts, since every
edge this script would insert already exists after the first run.

Touch point 6 (CORRECTION -> APPROVAL_REQUEST) reconstruction note: the
live touch point writes at submit() time, using the review run's resolved
scope AT THAT MOMENT (every issue whose correction has
final_value IS NOT NULL AND status IN ('ACCEPTED','EDITED')). That
moment-in-time state is not separately persisted anywhere for historical
approval_requests, so this script approximates it as: every correction in
the review run's resolved scope whose decided_at is <= the approval
request's requested_at. This is the closest available reconstruction from
existing data and matches submit()'s own _resolved_scope() query shape,
but is a best-effort proxy for approval_requests that predate Phase 10 —
flagged explicitly here rather than presented as an exact replay.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.core.database import SessionLocal
from app.modules.lineage.service import LineageService


def backfill(db) -> Counter:
    lineage = LineageService(db)
    counts: Counter = Counter()

    # --- Touch point 0: DATA_SOURCE -> CONNECTION -----------------------
    rows = db.execute(text("SELECT data_source_id, id FROM connections")).all()
    lineage.record_edges_bulk([("DATA_SOURCE", r[0], "CONNECTION", r[1], "DERIVED_FROM") for r in rows])
    counts["DERIVED_FROM (DATA_SOURCE->CONNECTION)"] += len(rows)

    # --- Touch point 1: CONNECTION -> SCHEMA -> DATASET -> COLUMN -------
    rows = db.execute(text("SELECT connection_id, id FROM schemas")).all()
    lineage.record_edges_bulk([("CONNECTION", r[0], "SCHEMA", r[1], "DERIVED_FROM") for r in rows])
    counts["DERIVED_FROM (CONNECTION->SCHEMA)"] += len(rows)

    rows = db.execute(text("SELECT schema_id, id FROM datasets")).all()
    lineage.record_edges_bulk([("SCHEMA", r[0], "DATASET", r[1], "DERIVED_FROM") for r in rows])
    counts["DERIVED_FROM (SCHEMA->DATASET)"] += len(rows)

    rows = db.execute(text("SELECT dataset_id, id FROM columns")).all()
    lineage.record_edges_bulk([("DATASET", r[0], "COLUMN", r[1], "DERIVED_FROM") for r in rows])
    counts["DERIVED_FROM (DATASET->COLUMN)"] += len(rows)

    # --- Touch point 2: DATASET -> PROFILE_RUN --------------------------
    rows = db.execute(text("SELECT dataset_id, id FROM profile_runs")).all()
    lineage.record_edges_bulk([("DATASET", r[0], "PROFILE_RUN", r[1], "PROFILED_BY") for r in rows])
    counts["PROFILED_BY (DATASET->PROFILE_RUN)"] += len(rows)

    # --- Touch point 3: DATASET -> VALIDATION_RUN -----------------------
    rows = db.execute(text("SELECT dataset_id, id FROM validation_runs")).all()
    lineage.record_edges_bulk([("DATASET", r[0], "VALIDATION_RUN", r[1], "VALIDATED_BY") for r in rows])
    counts["VALIDATED_BY (DATASET->VALIDATION_RUN)"] += len(rows)

    # --- Touch point 4: VALIDATION_RUN -> REVIEW_RUN, REVIEW_RUN -> ISSUE
    rows = db.execute(text("SELECT validation_run_id, id FROM review_runs")).all()
    lineage.record_edges_bulk([("VALIDATION_RUN", r[0], "REVIEW_RUN", r[1], "DERIVED_FROM") for r in rows])
    counts["DERIVED_FROM (VALIDATION_RUN->REVIEW_RUN)"] += len(rows)

    rows = db.execute(text("SELECT review_run_id, id FROM issues")).all()
    lineage.record_edges_bulk([("REVIEW_RUN", r[0], "ISSUE", r[1], "DERIVED_FROM") for r in rows])
    counts["DERIVED_FROM (REVIEW_RUN->ISSUE)"] += len(rows)

    # --- Touch point 5: ISSUE -> CORRECTION ------------------------------
    rows = db.execute(text("SELECT issue_id, id FROM corrections")).all()
    lineage.record_edges_bulk([("ISSUE", r[0], "CORRECTION", r[1], "CORRECTED_BY") for r in rows])
    counts["CORRECTED_BY (ISSUE->CORRECTION)"] += len(rows)

    # --- Touch point 6: CORRECTION -> APPROVAL_REQUEST (best-effort
    #     reconstruction of submission-time resolved scope; see docstring)
    rows = db.execute(
        text(
            """
            SELECT c.id, ar.id
            FROM approval_requests ar
            JOIN issues i ON i.review_run_id = ar.review_run_id
            JOIN corrections c ON c.issue_id = i.id
            WHERE c.final_value IS NOT NULL
              AND c.status IN ('ACCEPTED', 'EDITED')
              AND c.decided_at <= ar.requested_at
            """
        )
    ).all()
    lineage.record_edges_bulk([("CORRECTION", r[0], "APPROVAL_REQUEST", r[1], "APPROVED_BY") for r in rows])
    counts["APPROVED_BY (CORRECTION->APPROVAL_REQUEST)"] += len(rows)

    # --- Touch point 7: APPROVAL_REQUEST -> STAGING_RUN ------------------
    # staging_runs has no direct approval_request_id column — reconstructed
    # via the same "most recently CREATED APPROVED request as of this
    # staging_run's creation" rule StagingService.trigger() itself uses.
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (sr.id) ar.id, sr.id
            FROM staging_runs sr
            JOIN approval_requests ar
              ON ar.review_run_id = sr.review_run_id
             AND ar.status = 'APPROVED'
             AND ar.created_at <= sr.created_at
            ORDER BY sr.id, ar.created_at DESC
            """
        )
    ).all()
    lineage.record_edges_bulk([("APPROVAL_REQUEST", r[0], "STAGING_RUN", r[1], "STAGED_INTO") for r in rows])
    counts["STAGED_INTO (APPROVAL_REQUEST->STAGING_RUN)"] += len(rows)

    # --- Touch point 8: STAGING_RUN -> PUBLISH_RUN -----------------------
    rows = db.execute(text("SELECT staging_run_id, id FROM publish_runs")).all()
    lineage.record_edges_bulk([("STAGING_RUN", r[0], "PUBLISH_RUN", r[1], "PUBLISHED_TO") for r in rows])
    counts["PUBLISHED_TO (STAGING_RUN->PUBLISH_RUN)"] += len(rows)

    return counts


def main() -> None:
    db = SessionLocal()
    try:
        counts = backfill(db)
        db.commit()
        print("Lineage backfill complete. Edges examined per relationship_type (INSERT attempted, duplicates skipped via ON CONFLICT DO NOTHING):")
        for label, count in counts.items():
            print(f"  {label}: {count}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
