"""Phase 4.10 — targeted test for the minimal schema exposure change:
CorrectionSuggestionResponse now serializes the already-persisted
strategy/evidence_detail columns (see app/db/models/review.py's
CorrectionSuggestion.strategy/evidence_detail docstring, Phase 4.1). No
business logic under test here, only that the wire shape includes what
the frontend needs and stays None when the columns are unset.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.modules.review.schemas import CorrectionSuggestionResponse


@dataclass
class _FakeCorrectionSuggestion:
    """Mirrors app.db.models.review.CorrectionSuggestion's attribute shape
    closely enough for model_validate(..., from_attributes=True) without
    touching the database."""

    id: uuid.UUID
    issue_id: uuid.UUID
    source: str
    ai_suggestion_id: uuid.UUID | None
    suggested_value: str
    confidence: Decimal
    category: str
    fix_type: str
    reasoning: str | None
    is_selected: bool
    selected_by: uuid.UUID | None
    selected_at: datetime | None
    created_at: datetime
    strategy: str | None = None
    evidence_detail: dict | None = None


def _base_kwargs(**overrides):
    kwargs = dict(
        id=uuid.uuid4(),
        issue_id=uuid.uuid4(),
        source="AI",
        ai_suggestion_id=uuid.uuid4(),
        suggested_value="suresh.kumar@gmail.com",
        confidence=Decimal("1.0"),
        category="AI_HIGH_CONFIDENCE",
        fix_type="AI_PROPOSED",
        reasoning="Matches the dominant email template for this dataset.",
        is_selected=False,
        selected_by=None,
        selected_at=None,
        created_at=datetime.now(timezone.utc),
    )
    kwargs.update(overrides)
    return kwargs


def test_correction_suggestion_response_serializes_strategy_and_evidence_detail():
    evidence = {
        "available": True,
        "ambiguous": False,
        "strategies_attempted": ["TEMPLATE"],
        "strategies_agreeing": ["STRING_TEMPLATE"],
        "recommended_candidate": "suresh.kumar@gmail.com",
        "recommended_strategy": "STRING_TEMPLATE",
        "confidence": 1.0,
        "supporting_count": 10,
        "contradicting_count": 0,
        "reason": None,
    }
    model = _FakeCorrectionSuggestion(**_base_kwargs(strategy="STRING_TEMPLATE", evidence_detail=evidence))

    response = CorrectionSuggestionResponse.model_validate(model)

    assert response.strategy == "STRING_TEMPLATE"
    assert response.evidence_detail == evidence
    assert response.evidence_detail["supporting_count"] == 10
    assert response.evidence_detail["contradicting_count"] == 0


def test_correction_suggestion_response_defaults_strategy_and_evidence_detail_to_none():
    """RULE_BASED suggestions (and AI suggestions from before advanced
    inference ran) never set these columns — must round-trip as None, not
    a fabricated empty dict/string."""
    model = _FakeCorrectionSuggestion(**_base_kwargs(source="RULE_BASED", category="DETERMINISTIC"))

    response = CorrectionSuggestionResponse.model_validate(model)

    assert response.strategy is None
    assert response.evidence_detail is None
