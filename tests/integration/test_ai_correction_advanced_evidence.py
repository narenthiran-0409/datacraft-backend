"""Integration tests for Phase 4.5: wiring the four Phase 4 evidence
engines (relationship, sequence, template, temporal) + candidate
aggregation into the live AI correction workflow, behind
settings.AI_CORRECTION_ADVANCED_INFERENCE_ENABLED (default False).

Mirrors tests/integration/test_ai_correction_evidence.py's own
conventions exactly: the LLM call is always mocked (provider.send); the
SOURCE database is the real local Postgres test connection (pg_connection)
so most of these tests exercise the real Phase 4 evidence engines
end-to-end through AISuggestionService, not simulated — except where a
test explicitly needs to force a specific aggregation outcome (ambiguity,
LLM misbehavior) that would be impractical or fragile to construct through
hand-picked real data alone.
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
from app.modules.ai.candidates import Candidate, aggregate_candidates
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
        actor=admin_user, name=f"adv_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type=rule_type, origin="CUSTOM", definition=definition, severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    return RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=column_id, column_ids=None, template_id=None,
    )


def _discover_and_review(db, redis_client, admin_user, pg_connection, table_name, columns, rule_specs):
    """rule_specs: list of (column_name, rule_type, definition) tuples.
    Returns (review_run, dataset, issues_by_column)."""
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

    for col_name, rule_type, definition in rule_specs:
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == col_name)).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type=rule_type, definition=definition, column_id=col.id)

    validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name="adv_evidence_test", actor=admin_user
    )
    db.expire_all()

    issues_by_column = {}
    for col_name, _rt, _d in rule_specs:
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == col_name)).scalar_one()
        issues_by_column[col_name] = db.execute(
            select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)
        ).scalars().all()

    return review_run, dataset, issues_by_column


def _enable_advanced(monkeypatch, **overrides):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(settings, "AI_CORRECTION_ADVANCED_INFERENCE_ENABLED", True)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def _bridge_row(db: Session, issue_id) -> CorrectionSuggestion:
    return db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue_id)).scalar_one()


# ---------------------------------------------------------------------------
# 1. Relationship candidate integration (real engine, end-to-end)
# ---------------------------------------------------------------------------


def test_relationship_candidate_flows_through_advanced_path(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_rel_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',30,1470),(4,'Apple',15,-500)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("order_amount", "RANGE", {"min": 0, "max": 1000000})],
        )
        issue = issues_by_column["order_amount"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9,
            "reasoning": "Consistent ratio to Qty.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 735.0
        assert bridge.strategy == "ratio_consistency"
        assert bridge.evidence_detail is not None
        assert bridge.evidence_detail["recommended_candidate"] == "735"
        assert bridge.evidence_detail["recommended_strategy"] == "ratio_consistency"
        # RANGE is a genuine cross-column numeric-correction rule type —
        # RELATIONSHIP must still be attempted (Blocker 1 fix must not
        # over-correct into never running RELATIONSHIP at all).
        assert "RELATIONSHIP" in bridge.evidence_detail["strategies_attempted"]

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert "advanced_evidence" in suggestion.content
        assert "relationship_evidence" not in suggestion.content
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 2. Sequence candidate integration (real engine, end-to-end)
# ---------------------------------------------------------------------------


def test_sequence_candidate_flows_through_advanced_path(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_seq_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "1005", "confidence": 0.9,
            "reasoning": "Fills the single gap in an otherwise clean sequence.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "1005"
        assert bridge.strategy == "SEQUENCE_GAP"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 3. Template candidate integration (real engine, end-to-end)
# ---------------------------------------------------------------------------


def test_template_candidate_flows_through_advanced_path(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_tmpl_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@example.org'),"
                "(2,'Priya Raj','priya.raj@example.org'),"
                "(3,'Vijay Kumar','vijay.kumar@example.org'),"
                "(4,'Meena Devi','meena.devi@example.org'),"
                "(5,'Suresh Kumar','suresh-invalid-email')"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None,
            rule_specs=[("email", "PATTERN", {"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"})],
        )
        issue = issues_by_column["email"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "suresh.kumar@example.org", "confidence": 0.9,
            "reasoning": "Matches the observed name-to-email template.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "suresh.kumar@example.org"
        assert bridge.strategy == "STRING_TEMPLATE"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 4. Temporal candidate integration (real engine, end-to-end)
# ---------------------------------------------------------------------------


def test_temporal_candidate_flows_through_advanced_path(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_temporal_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, billing_date DATE)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'2026-01-31'),(2,'2026-02-28'),(3,'2026-03-31'),(4,NULL),(5,'2026-05-31'),(6,'2026-06-30')"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("billing_date", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["billing_date"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "2026-04-30", "confidence": 0.9,
            "reasoning": "End-of-month calendar progression.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "2026-04-30"
        assert bridge.strategy == "TEMPORAL_GAP"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 5 / 6. Cross-strategy agreement / conflict — aggregation-level (per spec,
# these mirror app.modules.ai.candidates' own already-tested behavior;
# confirmed again at this phase's own integration layer for documentation).
# ---------------------------------------------------------------------------


def test_identical_cross_strategy_candidates_strengthen_recommendation():
    a = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.9, supporting_count=3)
    b = Candidate(value="735", strategy="SEQUENCE_GAP", confidence=0.85, supporting_count=5)
    result = aggregate_candidates([a, b], strategies_attempted=["RELATIONSHIP", "SEQUENCE"])
    assert result.ambiguous is False
    assert result.recommended_candidate is not None
    assert result.recommended_candidate.value == "735"
    assert set(result.strategies_with_candidates) == {"RATIO_CONSISTENCY", "SEQUENCE_GAP"}


def test_conflicting_cross_strategy_candidates_is_ambiguous():
    a = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.9)
    b = Candidate(value="720", strategy="SEQUENCE_GAP", confidence=0.89)  # within the 0.03 ambiguity margin
    result = aggregate_candidates([a, b], strategies_attempted=["RELATIONSHIP", "SEQUENCE"])
    assert result.ambiguous is True
    assert result.recommended_candidate is None


def test_small_confidence_gap_does_not_blindly_defeat_contradicting_strategy():
    """A 0.01 confidence difference must not arbitrarily pick a winner when
    the two strategies disagree on the value."""
    a = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.91)
    b = Candidate(value="720", strategy="SEQUENCE_GAP", confidence=0.90)
    result = aggregate_candidates([a, b], strategies_attempted=["RELATIONSHIP", "SEQUENCE"])
    assert result.ambiguous is True
    assert result.recommended_candidate is None


def test_aggregation_conflict_flows_through_to_null_suggestion(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Forces a real two-strategy conflict through the actual service
    orchestration layer (not just candidates.py directly) by monkeypatching
    one engine's result for one specific issue — proves the live wiring
    correctly surfaces AMBIGUOUS end-to-end, not just at the pure-function
    level covered by the two tests above."""
    table_name = f"dq_adv_conflict_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        import app.modules.ai.suggestion_service as suggestion_service_module

        real_gather = suggestion_service_module.AISuggestionService._gather_advanced_evidence

        def _forced_conflict(self, *, issue, column, rule_type, dataset_id):
            aggregation, reason = real_gather(self, issue=issue, column=column, rule_type=rule_type, dataset_id=dataset_id)
            if aggregation is None or aggregation.recommended_candidate is None:
                return aggregation, reason
            from app.modules.ai.candidates import Candidate as _C, aggregate_candidates as _agg

            real_candidate = aggregation.recommended_candidate
            conflicting = _C(value=str(float(real_candidate.value) + 999), strategy="RELATIONSHIP_FORCED", confidence=real_candidate.confidence - 0.01)
            forced = _agg([real_candidate, conflicting], strategies_attempted=list(aggregation.strategies_attempted) + ["RELATIONSHIP"])
            return forced, None

        monkeypatch.setattr(
            suggestion_service_module.AISuggestionService, "_gather_advanced_evidence", _forced_conflict
        )

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "1005", "confidence": 0.9, "reasoning": "n/a",
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
# 7. No candidate
# ---------------------------------------------------------------------------


def test_no_candidate_when_no_strategy_finds_anything(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_nocandidate_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,101),(2,NULL),(3,317),(4,850),(5,204),(6,999)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {"category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None, "reasoning": "no pattern"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == ""
        assert bridge.evidence_detail["available"] is True
        assert bridge.evidence_detail["recommended_candidate"] is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 9 / 10 / 11. LLM agreement / value-change / invention
# ---------------------------------------------------------------------------


def test_llm_agreeing_with_backend_candidate_is_kept(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_agree_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {"category": "AI_HIGH_CONFIDENCE", "suggested_value": "1005", "confidence": 0.95, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "1005"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_llm_changing_backend_candidate_is_rejected(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_changed_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        # The AI claims a DIFFERENT value than the real deterministic candidate (1005).
        payload = {"category": "AI_HIGH_CONFIDENCE", "suggested_value": "9999", "confidence": 0.95, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        # The deterministic candidate is still surfaced — never left blank
        # just because the AI's own value was rejected.
        assert bridge.suggested_value == "1005"
        assert "9999" in bridge.reasoning
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_llm_inventing_candidate_without_backend_evidence_is_rejected(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_invent_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,101),(2,NULL),(3,317),(4,850),(5,204),(6,999)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        # No real backend evidence exists for this irregular column — but
        # the (mocked) LLM confidently invents a value anyway.
        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "500", "confidence": 0.8, "reasoning": "guess",
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
# 12 / 13. Candidate + NEEDS_REVIEW retains value / ambiguous -> null
# ---------------------------------------------------------------------------


def test_candidate_with_ai_needs_review_retains_suggested_value(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """The AI itself chooses NEEDS_REVIEW despite a real backend candidate
    existing — its own more conservative judgement is respected as the
    category, but the deterministic value is still shown (Phase 4.5's one
    intentional behavior change from Phase 1-3)."""
    table_name = f"dq_adv_needsreview_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None,
            "reasoning": "I'd like a human to confirm this gap-fill before it's applied.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "NEEDS_REVIEW"
        assert bridge.suggested_value == "1005"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ambiguous_aggregation_forces_null_suggested_value(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_ambig_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        # Two gaps -> AMBIGUOUS from the sequence engine itself.
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,1001),(2,1002),(3,NULL),(4,1004),(5,1007),(6,1008)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "1003", "confidence": 0.9, "reasoning": "n/a",
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
# 14 / 15. evidence_detail / strategy persisted
# ---------------------------------------------------------------------------


def test_evidence_detail_and_strategy_are_persisted(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_persist_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(f"INSERT INTO {table_name} VALUES (1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {"category": "AI_HIGH_CONFIDENCE", "suggested_value": "1005", "confidence": 0.9, "reasoning": "n/a"}
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.strategy == "SEQUENCE_GAP"
        assert bridge.evidence_detail["recommended_candidate"] == "1005"
        assert bridge.evidence_detail["recommended_strategy"] == "SEQUENCE_GAP"
        assert "SEQUENCE" in bridge.evidence_detail["strategies_attempted"]
        assert bridge.evidence_detail["ambiguous"] is False
        # Never raw comparable rows/pairs.
        assert "comparable_pairs" not in bridge.evidence_detail
        assert "observed_values" not in bridge.evidence_detail
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# BLOCKER 1 ACCEPTANCE — large clustered integers must not let RELATIONSHIP's
# constant_within_group shape (Phase 0, unmodified) spuriously compete with
# SEQUENCE for a COMPLETENESS/UNIQUENESS/DUPLICATE issue.
# ---------------------------------------------------------------------------


def test_large_clustered_integer_sequence_relationship_does_not_compete(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """The exact data named in the Phase 4.5 acceptance-fix spec:
    1001,1002,1003,1004,1006,1007,1008 (a COMPLETENESS issue on the null
    row). Without the applicability fix, RELATIONSHIP's
    constant_within_group shape spuriously "fits" this tightly-clustered
    column (small coefficient-of-variation relative to a large mean) and
    out-competes the correct SEQUENCE_GAP=1005 candidate on confidence.
    RELATIONSHIP must not even be attempted for a numeric COMPLETENESS
    issue — proven here by asserting "RELATIONSHIP" is absent from
    strategies_attempted entirely, not merely "did not win"."""
    table_name = f"dq_adv_blocker1_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, ref_no INT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("ref_no", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["ref_no"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "1005", "confidence": 0.9,
            "reasoning": "Fills the single gap in an otherwise clean sequence.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "1005"
        assert bridge.strategy == "SEQUENCE_GAP"
        assert bridge.evidence_detail["ambiguous"] is False
        assert "RELATIONSHIP" not in bridge.evidence_detail["strategies_attempted"]
        assert bridge.evidence_detail["strategies_attempted"] == ["SEQUENCE"]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_large_clustered_integer_sequence_with_arbitrary_column_name(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Identical scenario, deliberately generic column name — proving the
    applicability fix is driven by data_type/rule_type only, never by
    'ref_no' or any other name resembling an identifier."""
    table_name = f"dq_adv_blocker1_generic_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, attr_9 INT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,1001),(2,1002),(3,1003),(4,1004),(5,NULL),(6,1006),(7,1007),(8,1008)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("attr_9", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["attr_9"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "1005", "confidence": 0.9, "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "1005"
        assert bridge.strategy == "SEQUENCE_GAP"
        assert "RELATIONSHIP" not in bridge.evidence_detail["strategies_attempted"]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 16. Failing row excluded from its own training evidence
# ---------------------------------------------------------------------------


def test_failing_row_excluded_from_template_training(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """The failing row's own (invalid) target value must never be used as
    a training example — verified by putting an obviously-wrong pair at
    the failing row and confirming the learned template still comes out
    clean (unaffected by the garbage)."""
    table_name = f"dq_adv_exclude_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@example.org'),"
                "(2,'Priya Raj','priya.raj@example.org'),"
                "(3,'Vijay Kumar','vijay.kumar@example.org'),"
                "(4,'Meena Devi','meena.devi@example.org'),"
                "(5,'Suresh Kumar','totally-unrelated-garbage')"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None,
            rule_specs=[("email", "PATTERN", {"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"})],
        )
        issue = issues_by_column["email"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "suresh.kumar@example.org", "confidence": 0.9,
            "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        # If the garbage row had contaminated training, no clean template
        # would have been found at all (or a different one). The correct
        # template ("firstname.lastname@example.org") is still recovered.
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "suresh.kumar@example.org"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 17 / 18. Feature flag behavior
# ---------------------------------------------------------------------------


def test_flag_off_preserves_phase_1_3_behavior_unchanged(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """AI_CORRECTION_ADVANCED_INFERENCE_ENABLED left at its real default
    (False) while AI_CORRECTION_EVIDENCE_ENABLED is on — environment C:
    the OLD single-strategy relationship_evidence path must run exactly as
    before, never advanced_evidence."""
    table_name = f"dq_adv_flagoff_{uuid.uuid4().hex[:8]}"
    try:
        assert settings.AI_CORRECTION_ADVANCED_INFERENCE_ENABLED is False
        review_run, dataset, issue = None, None, None
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("order_amount", "RANGE", {"min": 0, "max": 1000000})],
        )
        issue = issues_by_column["order_amount"][0]

        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "AI_CORRECTION_EVIDENCE_ENABLED", True)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert float(bridge.suggested_value) == 735.0
        assert bridge.strategy is None  # Phase 4.1 columns stay unset on the old path
        assert bridge.evidence_detail is None

        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert "relationship_evidence" in suggestion.content
        assert "advanced_evidence" not in suggestion.content
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_flag_on_activates_advanced_path(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_flagon_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, product TEXT, qty INT, order_amount NUMERIC)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Apple',10,490),(2,'Apple',20,980),(3,'Apple',5,245),(4,'Apple',15,-500)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None, rule_specs=[("order_amount", "RANGE", {"min": 0, "max": 1000000})],
        )
        issue = issues_by_column["order_amount"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "735", "confidence": 0.9, "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.strategy is not None
        assert bridge.evidence_detail is not None
        suggestion = next(s for s in suggestions if s.id == bridge.ai_suggestion_id)
        db.refresh(suggestion)
        assert "advanced_evidence" in suggestion.content
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 19 / 20. Customer_Orders acceptance scenario (Suresh / Sara)
# ---------------------------------------------------------------------------


def test_customer_orders_suresh_scenario(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_co_suresh_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@gmail.com'),"
                "(2,'Priya Raj','priya.raj@gmail.com'),"
                "(3,'John Smith','john.smith@gmail.com'),"
                "(4,'Meena Devi','meena.devi@gmail.com'),"
                "(5,'David Wilson','david.wilson@gmail.com'),"
                "(6,'Vijay Kumar','vijay.kumar@gmail.com'),"
                "(7,'Anitha Raj','anitha.raj@gmail.com'),"
                "(8,'Ramesh Babu','ramesh.babu@gmail.com'),"
                "(9,'Suresh Kumar','suresh-invalid-email')"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None,
            rule_specs=[("email", "PATTERN", {"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"})],
        )
        issue = issues_by_column["email"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "suresh.kumar@gmail.com", "confidence": 0.92,
            "reasoning": "Matches the consistent firstname.lastname@gmail.com template.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "suresh.kumar@gmail.com"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_customer_orders_kavitha_missing_email_scenario(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Kavitha's email is NULL (missing, not malformed) — a COMPLETENESS
    violation rather than PATTERN. Candidate is produced only because the
    same template evidence is applicable to a missing value too."""
    table_name = f"dq_adv_co_kavitha_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@gmail.com'),"
                "(2,'Priya Raj','priya.raj@gmail.com'),"
                "(3,'John Smith','john.smith@gmail.com'),"
                "(4,'Meena Devi','meena.devi@gmail.com'),"
                "(5,'David Wilson','david.wilson@gmail.com'),"
                "(6,'Vijay Kumar','vijay.kumar@gmail.com'),"
                "(7,'Anitha Raj','anitha.raj@gmail.com'),"
                "(8,'Ramesh Babu','ramesh.babu@gmail.com'),"
                "(9,'Kavitha Rao',NULL)"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None,
            rule_specs=[("email", "COMPLETENESS", {"max_null_percentage": 0})],
        )
        issue = issues_by_column["email"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "kavitha.rao@gmail.com", "confidence": 0.9,
            "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "kavitha.rao@gmail.com"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_customer_orders_sara_scenario(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_co_sara_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@gmail.com'),"
                "(2,'Priya Raj','priya.raj@gmail.com'),"
                "(3,'John Smith','john.smith@gmail.com'),"
                "(4,'Meena Devi','meena.devi@gmail.com'),"
                "(5,'David Wilson','david.wilson@gmail.com'),"
                "(6,'Vijay Kumar','vijay.kumar@gmail.com'),"
                "(7,'Anitha Raj','anitha.raj@gmail.com'),"
                "(8,'Ramesh Babu','ramesh.babu@gmail.com'),"
                "(9,'Sara Thomas','sara-invalid')"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None,
            rule_specs=[("email", "PATTERN", {"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"})],
        )
        issue = issues_by_column["email"][0]

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "sara.thomas@gmail.com", "confidence": 0.92,
            "reasoning": "Matches the consistent firstname.lastname@gmail.com template.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge = _bridge_row(db, issue.id)
        assert bridge.category == "AI_HIGH_CONFIDENCE"
        assert bridge.suggested_value == "sara.thomas@gmail.com"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# 25. Privacy boundary — no raw rows in prompt/context
# ---------------------------------------------------------------------------


def test_no_raw_rows_reach_the_llm_in_advanced_mode(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_adv_privacy_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@example.org'),"
                "(2,'Priya Raj','priya.raj@example.org'),"
                "(3,'Vijay Kumar','vijay.kumar@example.org'),"
                "(4,'Meena Devi','meena.devi@example.org'),"
                "(5,'Suresh Kumar','suresh-invalid-email')"
            )
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        review_run, dataset, issues_by_column = _discover_and_review(
            db, redis_client, admin_user, pg_connection, table_name,
            columns=None,
            rule_specs=[("email", "PATTERN", {"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"})],
        )

        _create_prompt_version(db, admin_user)
        _enable_advanced(monkeypatch)

        payload = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "suresh.kumar@example.org", "confidence": 0.9,
            "reasoning": "n/a",
        }
        mock_cls = _mock_llm(payload)
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)

        sent_message = mock_cls.return_value.send.call_args.kwargs["messages"][0]
        sent_text = sent_message.content
        # Other people's real names/emails must never leak — only the
        # aggregate advanced_evidence block (recommended_candidate etc.)
        # and this issue's own failed_value/expected_value (already
        # visible to any reviewer, per the existing Phase 1 boundary).
        for forbidden in ("arun.kumar@example.org", "priya.raj@example.org", "Arun Kumar", "Priya Raj"):
            assert forbidden not in sent_text, f"leaked raw value: {forbidden!r}"
        assert "advanced_evidence" in sent_text
        for forbidden in ("password", "credential", "connection_string", "secret"):
            assert forbidden not in sent_text.lower()
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()
