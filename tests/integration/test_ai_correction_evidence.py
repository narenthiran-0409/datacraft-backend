"""Integration tests for Phase 1: wiring app.modules.ai.evidence into
AISuggestionService.generate_corrections(), behind
settings.AI_CORRECTION_EVIDENCE_ENABLED (default False).

The LLM call is always mocked (provider.send), exactly like the existing
tests in test_ai_suggestions.py. The SOURCE database is NOT mocked — the
pg_connection fixture points back at this project's own real local Postgres
test database, so evidence gathering's live provider.sample_rows() call is
genuinely exercised end-to-end, not simulated, except in the one test that
deliberately forces a source-layer failure.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    AIPromptVersion,
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    Schema,
    User,
)
from app.modules.ai.providers import ProviderResponse
from app.modules.ai.suggestion_service import AISuggestionService
from app.modules.discovery.tasks import run_discovery
from app.modules.jobs.service import JobsService
from app.modules.review.service import ReviewService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _create_prompt_version(db: Session, admin_user: User, prompt_key: str = "ai_correction") -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_llm(payload: dict):
    fake_response = ProviderResponse(
        content=json.dumps(payload), input_tokens=10, output_tokens=10, latency_ms=5, raw_metadata={}
    )
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _assign_rule(db: Session, admin_user: User, dataset: Dataset, *, rule_type: str, definition: dict, column_id):
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"evid_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type=rule_type, origin="CUSTOM", definition=definition, severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    return RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=column_id, column_ids=None, template_id=None,
    )


def _setup_review_with_issue(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, *,
    create_sql: str, insert_sql: str, rule_column: str, rule_type: str, rule_definition: dict,
):
    """Real discovery + real rule assignment + real validation + real review
    run, against a disposable Postgres table — mirrors the established
    pattern in test_ai_suggestions.py/_build_review_run_with_issue, made
    reusable across the different schemas this file needs."""
    db.execute(text(create_sql))
    db.execute(text(insert_sql))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    discover_job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(discover_job.id))
    db.expire_all()

    schema = db.execute(
        select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
    ).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    rule_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == rule_column)).scalar_one()

    _assign_rule(db, admin_user, dataset, rule_type=rule_type, definition=rule_definition, column_id=rule_col.id)

    validation_run, job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name="evidence_test", actor=admin_user
    )
    db.expire_all()

    issue = db.execute(
        select(Issue)
        .join(Column, Column.id == Issue.column_id)
        .where(Issue.review_run_id == review_run.id, Column.name == rule_column)
    ).scalars().first()
    return review_run, dataset, issue


def _enable_evidence(monkeypatch, *, min_group_size=3, min_fit_quality=0.9, sample_size=2000):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(settings, "AI_CORRECTION_EVIDENCE_ENABLED", True)
    monkeypatch.setattr(settings, "AI_CORRECTION_MIN_COMPARABLE_GROUP_SIZE", min_group_size)
    monkeypatch.setattr(settings, "AI_CORRECTION_MIN_FIT_QUALITY", min_fit_quality)
    monkeypatch.setattr(settings, "AI_CORRECTION_COMPARABLE_SAMPLE_SIZE", sample_size)


def _bridge_row(db: Session, issue_id) -> CorrectionSuggestion:
    return db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue_id)).scalar_one()


# ---------------------------------------------------------------------------
# 1. Feature flag OFF — existing behavior unchanged
# ---------------------------------------------------------------------------


def test_flag_off_produces_no_evidence_key_and_behaves_exactly_as_before(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_flagoff_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None
        assert settings.AI_CORRECTION_EVIDENCE_ENABLED is False  # the real default, not just this test's

        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "no evidence"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert "relationship_evidence" not in suggestion.content
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 2. The target Apple/Qty/OrderAmount scenario
# ---------------------------------------------------------------------------


def test_apple_scenario_evidence_backed_candidate_735_when_llm_agrees(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_apple_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.95,
            "reasoning": "OrderAmount is consistently ~49x Qty for Apple.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 735.0

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is True
        assert evidence["status"] == "CANDIDATE"
        assert evidence["relationship_type"] == "ratio_consistency"
        assert evidence["candidate_value"] == 735.0
        assert evidence["group_size"] == 3
        assert evidence["fit_quality"] == 1.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 3. Employee / Salary / Tax — whole-dataset ratio
# ---------------------------------------------------------------------------


def test_employee_salary_tax_whole_dataset_ratio(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_emp_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, employee TEXT, salary NUMERIC, tax NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Alice',1000,200),(2,'Bob',2000,400),(3,'Carol',1500,300),(4,'Dave',4000,-50)"
            ),
            rule_column="tax", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "800", "confidence": 0.9,
            "reasoning": "Tax is consistently 20% of Salary.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 800.0  # 0.2 * 4000
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 4. Invoice / Qty / UnitPrice / Discount / Total — discount breaks the
#    simple product relationship; must NOT become HIGH_CONFIDENCE regardless
#    of what the (mocked) LLM claims.
# ---------------------------------------------------------------------------


def test_invoice_discount_mismatch_never_becomes_high_confidence(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_invoice_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=(
                f"CREATE TABLE {table_name} "
                "(id INT PRIMARY KEY, qty NUMERIC, unit_price NUMERIC, discount NUMERIC, total NUMERIC)"
            ),
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,10,5,0.0,50),(2,20,5,0.0,100),(3,10,5,0.5,25),(4,10,5,0.3,-1)"
            ),
            rule_column="total", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch, min_fit_quality=0.9)

        # The (mocked) LLM confidently claims a value based on the naive
        # Qty*UnitPrice model (10*5=50) — the backend must reject this
        # regardless, since the deterministic evidence itself doesn't
        # clear the confidence bar once the discounted rows are involved.
        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "50", "confidence": 0.8,
            "reasoning": "Qty times UnitPrice.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 5. No relationship at all
# ---------------------------------------------------------------------------


def test_no_relationship_never_becomes_high_confidence(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_norel_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, cat TEXT, a INT, b INT, target INT)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'X',3,17,91),(2,'X',42,2,5),(3,'X',8,200,-13),(4,'X',71,9,1000),(5,'X',5,5,-1)"
            ),
            rule_column="target", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "42", "confidence": 0.9, "reasoning": "guess",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 6. Ambiguous relationships
# ---------------------------------------------------------------------------


def test_ambiguous_relationships_never_become_high_confidence(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_ambig_{uuid.uuid4().hex[:8]}"
    try:
        # target is both ~constant (~100) AND target/m1 is ~constant (~2) —
        # these disagree meaningfully for this failing row's m1=70.
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, cat TEXT, m1 INT, target INT)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'X',50,100),(2,'X',51,102),(3,'X',49,98),(4,'X',70,-1)"
            ),
            rule_column="target", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch, min_fit_quality=0.9)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "140", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
        assert "human review" in (bridge.reasoning or "").lower()
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 7. Insufficient comparable rows
# ---------------------------------------------------------------------------


def test_insufficient_comparable_rows_never_become_high_confidence(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_tiny_{uuid.uuid4().hex[:8]}"
    try:
        # Only 2 clean comparable rows for Cat='X' -> below min_group_size=3,
        # even though the fit would be mathematically perfect (ratio=10).
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, cat TEXT, m INT, target INT)",
            insert_sql=f"INSERT INTO {table_name} VALUES (1,'X',10,100),(2,'X',20,200),(3,'X',5,-1)",
            rule_column="target", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch, min_group_size=3)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "50", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert suggestion.content["relationship_evidence"]["status"] == "INSUFFICIENT_GROUP"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 8. Provider/source failure — batch must continue
# ---------------------------------------------------------------------------


def test_source_provider_failure_during_evidence_gathering_does_not_break_correction(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_srcfail_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        # Simulate a source-layer failure specifically inside evidence
        # gathering's own provider resolution — the correction pipeline's
        # own (mocked) LLM call must still succeed and produce a real
        # suggestion, proving evidence failures are fully isolated.
        import app.modules.ai.suggestion_service as suggestion_service_module

        def _broken_get_provider(*args, **kwargs):
            raise RuntimeError("simulated source connectivity failure")

        monkeypatch.setattr(suggestion_service_module, "get_provider", _broken_get_provider)

        payload = {
            "category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None,
            "reasoning": "No evidence was available.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert len(suggestions) == 1  # the batch completed normally, not aborted
        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert "simulated source connectivity failure" in evidence["reason"]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 9. Phase 3 fix: failing row missing from the bounded comparable sample no
#    longer blocks evidence — it's located by a targeted key lookup instead.
# ---------------------------------------------------------------------------


def test_failing_row_missing_from_bounded_sample_no_longer_blocks_evidence(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Phase 1's limitation (superseded): evidence used to require the
    failing row to appear inside the bounded comparable sample. Phase 3
    locates it via a targeted provider.fetch_rows_by_keys() lookup instead,
    so excluding it from the comparable sample must no longer matter."""
    table_name = f"dq_evid_notfound_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        # Wrap the real provider so sample_rows() (comparable rows only)
        # never returns the failing row (id=4) — simulates a large-table
        # bounded sample that happened to miss it. fetch_rows_by_keys() is
        # left untouched (real Postgres implementation), so the failing row
        # must still be found via the targeted path.
        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider

        class _MissingFailingRowWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def sample_rows(self, *args, **kwargs):
                result = self._inner.sample_rows(*args, **kwargs)
                filtered = [r for r in result.rows if r.get("id") != 4]
                return type(result)(rows=filtered, is_full_scan=result.is_full_scan)

        monkeypatch.setattr(
            suggestion_service_module, "get_provider",
            lambda *a, **k: _MissingFailingRowWrapper(real_get_provider(*a, **k)),
        )

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 735.0

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is True
        assert evidence["status"] == "CANDIDATE"
        assert evidence["candidate_value"] == 735.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 10. Privacy: raw comparable-row values must never reach the LLM
# ---------------------------------------------------------------------------


def test_raw_comparable_row_values_never_sent_to_the_orchestrator(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_privacy_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        mock_cls = _mock_llm(payload)
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        # Inspect exactly what was sent to provider.send() — the user-turn
        # message content is the rendered context.
        sent_message = mock_cls.return_value.send.call_args.kwargs["messages"][0]
        sent_text = sent_message.content

        # The evidence candidate (735) and the related column NAME (qty)
        # are expected/fine — but no OTHER comparable row's raw values
        # (other Qty/OrderAmount numbers, or the literal "Apple" string)
        # may appear anywhere in what was sent.
        for forbidden in ("980", "245", "\"Apple\"", "'Apple'"):
            assert forbidden not in sent_text, f"leaked raw value: {forbidden!r}"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 11. Candidate mismatch — AI value rejected, backend candidate preserved
# ---------------------------------------------------------------------------


def test_llm_value_disagreeing_with_deterministic_candidate_is_rejected(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_evid_mismatch_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        # The deterministic candidate is 735 — the mocked LLM instead claims
        # a wildly different, self-invented number.
        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "999999", "confidence": 0.95,
            "reasoning": "I have a hunch.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        # The AI's own value is rejected outright — never accepted, never
        # silently replaced by the deterministic candidate as if the AI
        # had proposed it either.
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
        assert "999999" in bridge.reasoning
        assert "735" in bridge.reasoning  # explains what the real evidence candidate was

        # But the deterministic evidence itself is preserved for the
        # reviewer, unaffected by the AI's rejected claim.
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["status"] == "CANDIDATE"
        assert evidence["candidate_value"] == 735.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# Phase 2 additions
# ---------------------------------------------------------------------------


def test_llm_proposes_700_backend_candidate_735_rejected_and_preserved(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """The exact scenario named in the Phase 2 spec: a plausible-looking but
    wrong LLM guess (700, close to but not equal to the real 735) must still
    be rejected — "close" is not "correct"; only an exact match (within
    float tolerance) is accepted."""
    table_name = f"dq_evid_p2mismatch_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "700", "confidence": 0.85,
            "reasoning": "Approximately 49 times qty.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
        assert "700" in bridge.reasoning
        assert "735" in bridge.reasoning

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert suggestion.content["relationship_evidence"]["candidate_value"] == 735.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_may_decline_a_candidate_evidence_offers_respecting_ai_judgement(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Strong deterministic evidence (status=CANDIDATE) does not force
    AI_HIGH_CONFIDENCE — if the (mocked) LLM itself judges NEEDS_REVIEW, the
    system must respect that, not override it upward just because a
    candidate was available."""
    table_name = f"dq_evid_p2decline_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        # The LLM sees the same CANDIDATE evidence as the "accepts" test but
        # chooses NEEDS_REVIEW anyway (its own judgement call).
        payload = {
            "category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None,
            "reasoning": "Only 3 comparable rows — I'd rather a human confirm this pattern first.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        # Evidence itself is still a real CANDIDATE — proving nothing forced
        # the AI's own (more conservative) decision to be overridden.
        assert suggestion.content["relationship_evidence"]["status"] == "CANDIDATE"
        assert suggestion.content["relationship_evidence"]["candidate_value"] == 735.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_llm_high_confidence_with_null_suggested_value_is_downgraded(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """An internally-inconsistent LLM response (claims AI_HIGH_CONFIDENCE
    but gives no suggested_value) must never surface as a value-bearing
    suggestion — self-corrects to NEEDS_REVIEW with no value, independent
    of the evidence-mismatch check (this is _parse_correction_response's
    own consistency guard, exercised here in the evidence-enabled path)."""
    table_name = f"dq_evid_p2nullval_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": None, "confidence": 0.9, "reasoning": "inconsistent",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_raw_comparable_row_values_never_reach_orchestrator_run_call(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Complementary, more direct privacy check than the provider-payload
    test above: intercepts AIOrchestratorService.run() itself and inspects
    the `context` dict argument directly — the boundary named explicitly in
    the Phase 2 spec ("must never reach AIOrchestratorService")."""
    table_name = f"dq_evid_p2privacy_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        from app.modules.ai.orchestrator_service import AIOrchestratorService

        captured_contexts = []
        real_run = AIOrchestratorService.run

        def _capturing_run(self, *, prompt_key, context, actor, **kwargs):
            captured_contexts.append(context)
            return real_run(self, prompt_key=prompt_key, context=context, actor=actor, **kwargs)

        monkeypatch.setattr(AIOrchestratorService, "run", _capturing_run)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)

        assert len(captured_contexts) == 1
        context = captured_contexts[0]
        evidence = context["relationship_evidence"]
        # Only aggregate fields — never raw comparable-row values.
        assert set(evidence.keys()) <= {
            "available", "status", "relationship_type", "group_size", "fit_quality",
            "residual", "coefficient_of_variation", "candidate_value", "related_columns", "reason",
        }
        blob = json.dumps(context)
        for forbidden in ("980", "245", "Apple"):
            assert forbidden not in blob, f"leaked raw value: {forbidden!r}"
        for forbidden in ("password", "credential", "connection_string", "secret"):
            assert forbidden not in blob.lower()
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_batch_isolation_one_issues_evidence_failure_does_not_break_another(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Two issues in the same review run; evidence gathering is forced to
    fail for one specific issue (simulated) while the other proceeds
    normally — both must still receive a real, independent outcome and the
    batch must complete in full."""
    table_name = f"dq_evid_p2batch_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(
            text(
                f"CREATE TABLE {table_name} "
                "(id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC, notes TEXT)"
            )
        )
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490,'ok'),(2,'Apple',20,980,'ok'),(3,'Apple',5,245,'ok'),"
                "(4,'Apple',15,-500,'bad amount'),(5,'Apple',7,300,NULL)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        jobs_service = JobsService(db, redis_client)
        discover_job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(discover_job.id))
        db.expire_all()

        schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
        ).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        amount_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "order_amount")
        ).scalar_one()
        notes_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "notes")
        ).scalar_one()

        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000000}, column_id=amount_col.id)
        _assign_rule(db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=notes_col.id)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="batch_isolation_test", actor=admin_user
        )
        db.expire_all()

        amount_issue = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == amount_col.id)
        ).scalar_one()
        notes_issue = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == notes_col.id)
        ).scalar_one()

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_gather = suggestion_service_module.AISuggestionService._gather_relationship_evidence

        def _flaky_gather(self, *, issue, column, dataset_id):
            if issue.id == amount_issue.id:
                raise RuntimeError("simulated evidence-gathering crash for this one issue")
            return real_gather(self, issue=issue, column=column, dataset_id=dataset_id)

        monkeypatch.setattr(
            suggestion_service_module.AISuggestionService, "_gather_relationship_evidence", _flaky_gather
        )

        payload = {
            "category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        # Both issues got a real outcome — the batch was not aborted.
        assert len(suggestions) == 2
        amount_bridge = _bridge_row(db, amount_issue.id)
        notes_bridge = _bridge_row(db, notes_issue.id)
        # The forced crash bypasses _gather_relationship_evidence's own
        # internal safety net (to prove the OUTER per-issue isolation in
        # generate_corrections() also holds), so this issue lands via that
        # outer handler's existing CANNOT_INFER path — while the other
        # issue in the SAME batch still completes normally via the mocked
        # LLM. Both landing with a real, terminal outcome (neither stuck,
        # neither aborting the other) is the actual property under test.
        assert amount_bridge.category == "CANNOT_INFER"
        assert notes_bridge.category == "NEEDS_REVIEW"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# Phase 3 additions: targeted row retrieval for reliable evidence
# ---------------------------------------------------------------------------


def test_targeted_retrieval_calls_fetch_rows_by_keys_with_correct_single_column_key(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Proves the failing row is now located via a targeted, batched
    provider.fetch_rows_by_keys() call keyed by the dataset's real primary
    key — not by searching the comparable sample."""
    table_name = f"dq_evid_p3single_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        fetch_calls = []

        class _SpyWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def fetch_rows_by_keys(self, schema, table, keys):
                fetch_calls.append((schema, table, keys))
                return self._inner.fetch_rows_by_keys(schema, table, keys)

        monkeypatch.setattr(
            suggestion_service_module, "get_provider",
            lambda *a, **k: _SpyWrapper(real_get_provider(*a, **k)),
        )

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        # Exactly one targeted lookup, for exactly the failing record (id=4)
        # — never a batch of many, never a scan.
        assert len(fetch_calls) == 1
        _schema_arg, table_arg, keys_arg = fetch_calls[0]
        assert table_arg == table_name
        assert keys_arg == [{"id": "4"}]

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 735.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_composite_key_targeted_retrieval_succeeds(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """A COMPOSITE key strategy (2-column primary key) must be supported for
    targeted retrieval, not just SINGLE_COLUMN."""
    table_name = f"dq_evid_p3composite_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(
            text(
                f"CREATE TABLE {table_name} "
                "(region_id INT, order_id INT, product TEXT, qty INT, order_amount NUMERIC, "
                "PRIMARY KEY (region_id, order_id))"
            )
        )
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,1,'Apple',10,490),(1,2,'Apple',20,980),(1,3,'Apple',5,245),(1,4,'Apple',15,-500)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        jobs_service = JobsService(db, redis_client)
        discover_job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(discover_job.id))
        db.expire_all()

        schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
        ).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        assert dataset.key_strategy == "COMPOSITE"

        amount_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "order_amount")
        ).scalar_one()
        _assign_rule(
            db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000000}, column_id=amount_col.id
        )

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="composite_test", actor=admin_user
        )
        db.expire_all()

        issue = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == amount_col.id)
        ).scalar_one()

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 735.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_provider_without_targeted_retrieval_support_returns_evidence_unavailable(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Simulates a provider that raises NotImplementedError from
    fetch_rows_by_keys (the real, documented behavior of MySQL/Oracle/SAP
    HANA/SQL Server today) — evidence must become unavailable, and there
    must be NO fallback to a bounded-sample search or any other scan."""
    table_name = f"dq_evid_p3unsupported_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        sample_calls = []

        class _UnsupportedTargetedRetrievalWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def fetch_rows_by_keys(self, *a, **k):
                raise NotImplementedError("Implemented in a later phase")

            def sample_rows(self, *a, **k):
                sample_calls.append((a, k))
                return self._inner.sample_rows(*a, **k)

        monkeypatch.setattr(
            suggestion_service_module, "get_provider",
            lambda *a, **k: _UnsupportedTargetedRetrievalWrapper(real_get_provider(*a, **k)),
        )

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert evidence["reason"] == "targeted_row_retrieval_not_supported_by_provider"
        assert sample_calls == []  # never falls back to a scan of any kind
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_row_index_fallback_dataset_never_calls_get_provider(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """A dataset with no real key (ROW_INDEX_FALLBACK) must skip evidence
    gathering entirely, before any credential/provider is even touched —
    row position can't be trusted to identify a specific record."""
    table_name = f"dq_evid_p3nopk_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (product TEXT, qty INT, order_amount NUMERIC)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "('Apple',10,490),('Apple',20,980),('Apple',5,245),('Apple',15,-500)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        jobs_service = JobsService(db, redis_client)
        discover_job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(discover_job.id))
        db.expire_all()

        schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
        ).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        assert dataset.key_strategy == "ROW_INDEX_FALLBACK"

        amount_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "order_amount")
        ).scalar_one()
        _assign_rule(
            db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000000}, column_id=amount_col.id
        )

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="nopk_test", actor=admin_user
        )
        db.expire_all()

        issue = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == amount_col.id)
        ).scalar_one()

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        get_provider_calls = []

        def _spy_get_provider(*a, **k):
            get_provider_calls.append((a, k))
            return real_get_provider(*a, **k)

        monkeypatch.setattr(suggestion_service_module, "get_provider", _spy_get_provider)

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert get_provider_calls == []  # no connection ever attempted

        bridge = _bridge_row(db, issue.id)
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert evidence["reason"] == "no_reliable_key_strategy_for_targeted_row_retrieval"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_malformed_composite_record_ref_returns_evidence_unavailable(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """A COMPOSITE-keyed dataset whose stored record_ref doesn't actually
    contain enough parts to reconstruct every key column (corruption, or a
    mismatch introduced elsewhere) must be treated as evidence-unavailable,
    never silently truncated into a wrong/partial lookup key."""
    table_name = f"dq_evid_p3malformed_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(
            text(
                f"CREATE TABLE {table_name} "
                "(region_id INT, order_id INT, product TEXT, qty INT, order_amount NUMERIC, "
                "PRIMARY KEY (region_id, order_id))"
            )
        )
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,1,'Apple',10,490),(1,2,'Apple',20,980),(1,3,'Apple',5,245),(1,4,'Apple',15,-500)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        jobs_service = JobsService(db, redis_client)
        discover_job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(discover_job.id))
        db.expire_all()

        schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
        ).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        assert dataset.key_strategy == "COMPOSITE"

        amount_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "order_amount")
        ).scalar_one()
        _assign_rule(
            db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000000}, column_id=amount_col.id
        )

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="malformed_test", actor=admin_user
        )
        db.expire_all()

        issue = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == amount_col.id)
        ).scalar_one()

        # Corrupt the persisted record_ref: COMPOSITE needs 2 unit-separator
        # -joined parts (one per key column) — truncate to a single part.
        issue.record_ref = "1"
        db.add(issue)
        db.commit()
        db.expire_all()
        issue = db.get(Issue, issue.id)

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert evidence["reason"] == "malformed_or_null_record_reference"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_record_deleted_before_correction_returns_record_not_found(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """The failing row may legitimately no longer exist at the source by the
    time correction generation runs (deleted or corrected out-of-band) —
    targeted retrieval must report this cleanly rather than crash or use a
    stale/wrong row."""
    table_name = f"dq_evid_p3deleted_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        db.execute(text(f"DELETE FROM {table_name} WHERE id = 4"))
        db.commit()

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert len(suggestions) == 1
        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert evidence["reason"] == "record_not_found_at_source"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_unexpected_exception_during_targeted_fetch_is_isolated(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """A non-NotImplementedError exception from fetch_rows_by_keys (e.g. a
    connection timeout) must still be fully isolated — evidence unavailable,
    correction generation continues, and the comparable-row sample is never
    fetched since the row lookup never got that far."""
    table_name = f"dq_evid_p3crash_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        sample_calls = []

        class _CrashingFetchWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def fetch_rows_by_keys(self, *a, **k):
                raise RuntimeError("simulated connection timeout")

            def sample_rows(self, *a, **k):
                sample_calls.append((a, k))
                return self._inner.sample_rows(*a, **k)

        monkeypatch.setattr(
            suggestion_service_module, "get_provider",
            lambda *a, **k: _CrashingFetchWrapper(real_get_provider(*a, **k)),
        )

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert len(suggestions) == 1
        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert "simulated connection timeout" in evidence["reason"]
        assert sample_calls == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_comparable_sample_size_passed_through_unchanged_after_targeted_retrieval(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Comparable-row retrieval must stay on the existing bounded
    provider.sample_rows() path, at the configured size, unaffected by the
    switch to targeted retrieval for the failing row itself."""
    table_name = f"dq_evid_p3boundedsample_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch, sample_size=17)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        sample_calls = []

        class _SpySampleWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def sample_rows(self, schema, table, sample_size, **kwargs):
                sample_calls.append(sample_size)
                return self._inner.sample_rows(schema, table, sample_size, **kwargs)

        monkeypatch.setattr(
            suggestion_service_module, "get_provider",
            lambda *a, **k: _SpySampleWrapper(real_get_provider(*a, **k)),
        )

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert sample_calls == [17]
        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_targeted_fetch_raw_failing_row_values_never_reach_llm(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Privacy check specific to Phase 3: a value that exists ONLY in the
    failing row (fetched via the new targeted-retrieval path, not the
    bounded sample) must never appear in what's sent to the LLM or in the
    stored suggestion content — only the aggregate relationship_evidence
    summary is allowed to reach either."""
    table_name = f"dq_evid_p3privacy2_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=(
                f"CREATE TABLE {table_name} "
                "(id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC, secret_note TEXT)"
            ),
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490,'n/a'),(2,'Apple',20,980,'n/a'),(3,'Apple',5,245,'n/a'),"
                "(4,'Apple',15,-500,'TARGETED_FETCH_SECRET_MARKER')"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "ratio",
        }
        mock_cls = _mock_llm(payload)
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        sent_message = mock_cls.return_value.send.call_args.kwargs["messages"][0]
        assert "TARGETED_FETCH_SECRET_MARKER" not in sent_message.content

        bridge = _bridge_row(db, issue.id)
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert "TARGETED_FETCH_SECRET_MARKER" not in json.dumps(suggestion.content)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_flag_off_never_attempts_targeted_retrieval_or_any_source_query(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Regression: with the flag at its real default (off), Phase 3's new
    targeted-retrieval code path must never even resolve a provider —
    identical to Phase 1's guarantee, now re-verified under the new code."""
    table_name = f"dq_evid_p3flagoff_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None
        assert settings.AI_CORRECTION_EVIDENCE_ENABLED is False

        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        get_provider_calls = []

        def _spy_get_provider(*a, **k):
            get_provider_calls.append((a, k))
            return real_get_provider(*a, **k)

        monkeypatch.setattr(suggestion_service_module, "get_provider", _spy_get_provider)

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "no evidence"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert get_provider_calls == []
        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_multiple_issues_each_receive_independent_targeted_fetch_call(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Two different failing rows in the same review run must each resolve
    their OWN targeted lookup with their own key — never batched together
    across issues, never confused with one another."""
    table_name = f"dq_evid_p3multi_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500),(5,'Apple',30,-9999)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        jobs_service = JobsService(db, redis_client)
        discover_job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(discover_job.id))
        db.expire_all()

        schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
        ).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        amount_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "order_amount")
        ).scalar_one()
        _assign_rule(
            db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000000}, column_id=amount_col.id
        )

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="multi_issue_test", actor=admin_user
        )
        db.expire_all()

        issues = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == amount_col.id)
        ).scalars().all()
        assert len(issues) == 2  # id=4 (-500) and id=5 (-9999) both violate the RANGE rule

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_get_provider = suggestion_service_module.get_provider
        fetch_calls = []

        class _SpyWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def fetch_rows_by_keys(self, schema, table, keys):
                fetch_calls.append(tuple(sorted(keys[0].items())))
                return self._inner.fetch_rows_by_keys(schema, table, keys)

        monkeypatch.setattr(
            suggestion_service_module, "get_provider",
            lambda *a, **k: _SpyWrapper(real_get_provider(*a, **k)),
        )

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert len(suggestions) == 2
        assert len(fetch_calls) == 2
        assert set(fetch_calls) == {(("id", "4"),), (("id", "5"),)}
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_vault_credential_resolution_failure_is_isolated(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Credential resolution failing (vault unavailable) at the point
    targeted retrieval needs it must be isolated exactly like every other
    evidence-gathering failure mode — never breaks correction generation."""
    table_name = f"dq_evid_p3vaultfail_{uuid.uuid4().hex[:8]}"
    try:
        review_run, dataset, issue = _setup_review_with_issue(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            ),
            rule_column="order_amount", rule_type="RANGE", rule_definition={"min": 0, "max": 1000000},
        )
        assert issue is not None

        _create_prompt_version(db, admin_user)
        _enable_evidence(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        def _broken_resolve(self, credential_ref):
            raise RuntimeError("simulated vault unavailable")

        monkeypatch.setattr(suggestion_service_module.LocalRedisVaultClient, "resolve", _broken_resolve)

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        assert len(suggestions) == 1
        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        evidence = suggestion.content["relationship_evidence"]
        assert evidence["available"] is False
        assert "simulated vault unavailable" in evidence["reason"]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()
