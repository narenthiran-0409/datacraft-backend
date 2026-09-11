"""Whole-dataset rule detection: a hybrid of fast, deterministic
pattern-matching (column name + value-pattern + stat-ratio heuristics, no
LLM call) and an LLM-backed fallback for columns the heuristics aren't
confident about. Extends, not replaces, the existing single-rule AI
recommendation path (AISuggestionService.generate_rule_recommendations,
which this module is the only caller of) and the manual rule-creation API
(RulesService.create_rule, which this module calls for every candidate it
produces — from either half).

Ported technique, not ported code: the confidence-scored heuristic below
(combining column NAME token matching, VALUE pattern matching, and simple
statistical ratios) is the same core idea as a similar detector from a
different project, rewritten from scratch against this project's actual
data shape. Two deliberate adaptations from that reference:

1. This project doesn't hold a client-side data frame — only
   SourceDatabaseProvider (live, requires a source connection) and
   Profiling's already-computed, already-stored column_profiles rows. This
   detector does neither a fresh source query nor a fresh profiling run;
   it reads the dataset's most recent COMPLETED profile_run's stats
   as-is. A column that has never been profiled still gets a (weaker,
   name/type-only) categorization rather than being skipped.
2. Column type is already known and authoritative here (Discovery already
   populates columns.normalized_data_type) — used directly instead of
   re-deriving a numeric/datetime ratio from raw values the way the
   reference project (working from an untyped pandas frame) had to.

HARD REQUIREMENT: nothing this module produces — pattern-matched or
AI-recommended — is ever active on creation. Every rule it creates goes
through RulesService.create_rule(), which structurally forces
status='PENDING_REVIEW' for origin in {PATTERN_DETECTED, AI_RECOMMENDED}
regardless of what's requested. This module never creates a
rule_assignment, so even a PENDING_REVIEW rule this module created cannot
be picked up by a validation run — that requires a separate, explicit,
human-driven rule_assignments.manage action after the rule is promoted.
"""
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    AIDisabledError,
    AIProviderUnavailableError,
    AIResponseInvalidError,
    DatasetNotActiveError,
    DatasetNotFoundError,
)
from app.db.models import Column, ColumnProfile, Dataset, ProfileRun, Rule, User
from app.modules.ai.suggestion_service import AISuggestionService
from app.modules.rules.service import SUPPORTED_RULE_TYPES, RulesService

# Confidence at/above which a pattern-matched category is trusted enough to
# create a PENDING_REVIEW rule directly, with no LLM call. Sits strictly
# between this module's lowest deliberately-confident score (0.80, a
# numeric-typed column with no name hint) and its catch-all default (0.55,
# free-text/no signal) — see _infer_category.
CONFIDENCE_THRESHOLD = 0.75

# Caps how many low-confidence columns go into a single AI-fallback call
# for very wide datasets. Each column's context entry (name, two data-type
# strings, four small numbers) is roughly 30-40 tokens once serialized;
# 40 columns keeps the whole batched prompt (system template + all column
# entries) comfortably under AI_MAX_CONTEXT_TOKENS (8000 by default) with
# real margin for the model's own response. Columns beyond the cap are
# reported back as skipped in the job result, never silently dropped or
# silently truncated into an incomplete prompt.
MAX_AI_FALLBACK_COLUMNS = 40

_ID_NAME_TOKENS = ("id", "code", "key", "number", "no")
_NUMERIC_NAME_TOKENS = ("amount", "price", "cost", "balance", "score", "total", "qty", "quantity")
_EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_REGEX = re.compile(r"^\+?[0-9][0-9\-\s]{7,18}$")
_HIGH_UNIQUENESS_RATIO = 0.75


@dataclass(frozen=True)
class CategoryDetection:
    category: str
    confidence: float
    reasoning: str


@dataclass(frozen=True)
class DetectedRule:
    rule: Rule
    column_id: uuid.UUID
    column_name: str
    method: str  # "PATTERN_MATCH" or "AI_RECOMMENDED"
    confidence: float | None
    reasoning: str | None


@dataclass
class DetectionResult:
    dataset_id: uuid.UUID
    pattern_detected: list[DetectedRule] = field(default_factory=list)
    ai_recommended: list[DetectedRule] = field(default_factory=list)
    ai_fallback_columns_considered: int = 0
    ai_fallback_columns_capped: int = 0
    ai_skipped_reason: str | None = None


def _sample_texts_and_weights(profile: ColumnProfile | None) -> tuple[list[str], list[int]]:
    """Real, already-stored, capped sample values from Profiling — never a
    fresh source query (this module does no I/O of its own). Purely
    server-side use: these values are deliberately never included in the
    AI-fallback context (see app.modules.ai.context.
    build_rule_recommendation_context's docstring for why)."""
    if profile is None:
        return [], []
    texts: list[str] = []
    weights: list[int] = []
    if profile.value_distribution:
        for entry in profile.value_distribution:
            value = entry.get("value")
            if value is not None:
                texts.append(str(value))
                weights.append(int(entry.get("count") or 1))
    else:
        for value in (profile.min_value, profile.max_value, profile.mode_value):
            if value is not None:
                texts.append(str(value))
                weights.append(1)
    return texts, weights


def _weighted_match_ratio(texts: list[str], weights: list[int], pattern: re.Pattern) -> float:
    """Fraction of observed (weighted) samples matching pattern. With
    value_distribution available this is an honest ratio over real
    per-value frequencies, not just a count of distinct samples — a value
    seen 40 times counts 40x a value seen once, the same spirit as the
    reference technique's ratio-over-a-real-sample, adapted to the
    smaller, pre-aggregated sample this project actually has stored."""
    total = sum(weights)
    if total == 0:
        return 0.0
    matched = sum(w for t, w in zip(texts, weights) if pattern.match(t))
    return matched / total


def _name_has_token(lowered_name: str, tokens: tuple[str, ...]) -> bool:
    """Exact-word token match against the name split on non-alphanumeric
    characters, not a bare substring check — plain `t in lowered_name`
    matches "no" inside "notes", "id" inside "avoid", or "key" inside
    "turkey". (regex \\b word boundaries don't fix this either: "_" is a
    \\w character, so "acct_no" has no \\b between "_" and "no" — splitting
    is what's actually needed.) Column names in this codebase are
    snake_case (Discovery reads them straight from the source schema), so
    splitting on underscores/digits reliably separates "acct_no" or
    "customer_id" (real matches) from "notes" (no real word boundary)."""
    words = re.split(r"[^a-z0-9]+", lowered_name)
    return any(w in tokens for w in words)


def _infer_category(column: Column, profile: ColumnProfile | None) -> CategoryDetection:
    lowered_name = column.name.lower()
    native_type = (column.normalized_data_type or "").upper()
    texts, weights = _sample_texts_and_weights(profile)

    if _name_has_token(lowered_name, ("email",)) or _weighted_match_ratio(texts, weights, _EMAIL_REGEX) > 0.8:
        return CategoryDetection("email", 0.96, "Column name or sampled values match an email address pattern.")

    if (
        _name_has_token(lowered_name, ("phone", "mobile", "contact", "tel"))
        or _weighted_match_ratio(texts, weights, _PHONE_REGEX) > 0.8
    ):
        return CategoryDetection("phone", 0.92, "Column name or sampled values match a phone/mobile pattern.")

    if native_type in ("DATE", "DATETIME"):
        return CategoryDetection("date", 0.90, f"Column's discovered data type is {native_type}.")

    distinct_ratio = (
        float(profile.distinct_percentage) / 100.0
        if profile is not None and profile.distinct_percentage is not None
        else None
    )
    looks_id_by_name = _name_has_token(lowered_name, _ID_NAME_TOKENS)
    looks_id_by_uniqueness = distinct_ratio is not None and distinct_ratio > _HIGH_UNIQUENESS_RATIO

    if looks_id_by_name and (looks_id_by_uniqueness or distinct_ratio is None):
        reasoning = "Column name indicates an identifier or code"
        reasoning += (
            f", and {distinct_ratio:.0%} of sampled values are distinct."
            if distinct_ratio is not None
            else " (no profiling stats yet to confirm uniqueness)."
        )
        return CategoryDetection("id", 0.85, reasoning)
    if looks_id_by_uniqueness:
        return CategoryDetection(
            "id", 0.80, f"{distinct_ratio:.0%} of sampled values are distinct, indicating an identifier or code."
        )

    if native_type in ("INTEGER", "DECIMAL"):
        if _name_has_token(lowered_name, _NUMERIC_NAME_TOKENS):
            return CategoryDetection(
                "numeric", 0.88, "Column name and discovered numeric data type indicate a bounded numeric measure."
            )
        return CategoryDetection("numeric", 0.80, f"Column's discovered data type is {native_type}.")

    return CategoryDetection(
        "free_text", 0.55, "No confident semantic category detected from name, type, or sampled values."
    )


def _rule_definition_for_category(
    category: str, profile: ColumnProfile | None
) -> tuple[str, dict] | None:
    """Maps a detected category to one of the six real rule types plus a
    definition matching that type's actual evaluator (app.modules.
    validation.engine) — not the reference project's own rule vocabulary.
    Returns None when the category doesn't map to a confident local rule
    (free_text always; numeric without usable min/max stats) — the caller
    routes those columns to the AI fallback instead.

    Deliberate deviation from the suggested mapping: "date" does not map
    to a PATTERN format-regex here. This project's DATE/DATETIME columns
    are already typed at discovery (native to the source schema) — the
    format is already guaranteed by the column's real type, so a PATTERN
    regex on a DATE column would either be redundant or actively fragile
    (evaluate_pattern str()-converts the value first, so it would be
    matching against a Python date/datetime repr, not source text).
    COMPLETENESS (never blank) is the check that's actually meaningful
    and honestly recommendable for a date/datetime column.

    "id"/high-uniqueness columns map to UNIQUENESS, not DUPLICATE or
    CROSS_COLUMN — this heuristic is single-column by construction and has
    no way to honestly detect composite near-duplicate rows or a
    relationship between two specific columns. DUPLICATE and CROSS_COLUMN
    are reachable only through the AI fallback, which can reason across
    all of a dataset's columns in one call rather than one column at a
    time.
    """
    if category == "email":
        return "PATTERN", {"regex": _EMAIL_REGEX.pattern}
    if category == "phone":
        return "PATTERN", {"regex": _PHONE_REGEX.pattern}
    if category == "date":
        return "COMPLETENESS", {"max_null_percentage": 0}
    if category == "id":
        return "UNIQUENESS", {"max_duplicate_percentage": 0}
    if category == "numeric":
        if profile is None or profile.min_value is None or profile.max_value is None:
            return None
        try:
            lo, hi = float(profile.min_value), float(profile.max_value)
        except (TypeError, ValueError):
            return None
        return "RANGE", {"min": lo, "max": hi}
    return None


class RuleDetectionService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._rules_service = RulesService(db)
        self._ai_suggestions = AISuggestionService(db)

    def _get_active_dataset(self, dataset_id: uuid.UUID) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        if not dataset.is_active:
            raise DatasetNotActiveError(f"Dataset {dataset_id} is not active")
        return dataset

    def _latest_profiles_by_column(self, dataset_id: uuid.UUID) -> dict[uuid.UUID, ColumnProfile]:
        latest_run = self._db.execute(
            select(ProfileRun)
            .where(ProfileRun.dataset_id == dataset_id, ProfileRun.status == "COMPLETED")
            .order_by(ProfileRun.created_at.desc())
        ).scalars().first()
        if latest_run is None:
            return {}
        profiles = self._db.execute(
            select(ColumnProfile).where(ColumnProfile.profile_run_id == latest_run.id)
        ).scalars().all()
        return {p.column_id: p for p in profiles}

    def _create_candidate_rule(
        self, *, actor: User, dataset: Dataset, column: Column, rule_type: str, definition: dict,
        origin: str, category: str | None, confidence: float | None, reasoning: str | None,
        ai_suggestion_id: uuid.UUID | None = None,
    ) -> Rule:
        # A short random suffix, not a dedup check against prior runs:
        # rules.name is globally unique, and re-running detection on a
        # dataset that already has a candidate for a given column is a
        # legitimate thing to do (new profiling data, a schema change) —
        # the reviewer sees and dismisses genuine duplicates through the
        # normal promote/dismiss workflow rather than this module silently
        # deciding what counts as "already covered".
        name = f"{column.name}: {rule_type} ({category or 'ai'}) [{uuid.uuid4().hex[:6]}]"
        detected_for = {
            "dataset_id": str(dataset.id), "column_id": str(column.id), "column_name": column.name,
            "confidence": confidence,
        }
        if ai_suggestion_id is not None:
            detected_for["ai_suggestion_id"] = str(ai_suggestion_id)

        return self._rules_service.create_rule(
            actor=actor, name=name, description=reasoning, category=category, rule_type=rule_type,
            origin=origin, definition={**definition, "_detected_for": detected_for}, severity="MEDIUM",
            error_message_template=None,
        )

    def detect_for_dataset(self, dataset_id: uuid.UUID, actor: User) -> DetectionResult:
        dataset = self._get_active_dataset(dataset_id)
        columns = list(
            self._db.execute(
                select(Column)
                .where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
                .order_by(Column.ordinal_position)
            ).scalars()
        )
        profiles_by_column = self._latest_profiles_by_column(dataset.id)

        result = DetectionResult(dataset_id=dataset.id)
        ai_candidates: list[tuple[Column, ColumnProfile | None]] = []

        for column in columns:
            profile = profiles_by_column.get(column.id)
            detection = _infer_category(column, profile)

            rule_def = (
                _rule_definition_for_category(detection.category, profile)
                if detection.confidence >= CONFIDENCE_THRESHOLD
                else None
            )

            if rule_def is not None:
                rule_type, definition = rule_def
                rule = self._create_candidate_rule(
                    actor=actor, dataset=dataset, column=column, rule_type=rule_type, definition=definition,
                    origin="PATTERN_DETECTED", category=detection.category, confidence=detection.confidence,
                    reasoning=detection.reasoning,
                )
                column.semantic_category = detection.category
                column.semantic_category_source = "RULE_BASED"
                result.pattern_detected.append(
                    DetectedRule(
                        rule=rule, column_id=column.id, column_name=column.name, method="PATTERN_MATCH",
                        confidence=detection.confidence, reasoning=detection.reasoning,
                    )
                )
            else:
                ai_candidates.append((column, profile))

        result.ai_fallback_columns_considered = len(ai_candidates)
        if ai_candidates:
            capped = ai_candidates[: MAX_AI_FALLBACK_COLUMNS]
            result.ai_fallback_columns_capped = len(ai_candidates) - len(capped)

            try:
                suggestion, recommendations = self._ai_suggestions.generate_rule_recommendations(
                    dataset=dataset, columns_with_profiles=capped, actor=actor,
                )
            except (AIDisabledError, AIProviderUnavailableError, AIResponseInvalidError) as exc:
                # Graceful degradation, not a failed job: the pattern half
                # already succeeded for whatever it was confident about.
                # An unavailable/misconfigured/misbehaving LLM shouldn't
                # discard that real, already-committed work.
                result.ai_skipped_reason = f"{type(exc).__name__}: {exc}"
            else:
                columns_by_name = {column.name: column for column, _ in capped}
                for rec in recommendations:
                    column = columns_by_name.get(rec.get("column_name"))
                    if column is None:
                        continue  # model referenced a column we didn't ask about — ignore, never guess
                    rule_type = rec.get("rule_type")
                    if rule_type not in SUPPORTED_RULE_TYPES:
                        continue
                    definition = rec.get("definition")
                    if not isinstance(definition, dict):
                        continue
                    confidence = rec.get("confidence")
                    reasoning = rec.get("reasoning")

                    rule = self._create_candidate_rule(
                        actor=actor, dataset=dataset, column=column, rule_type=rule_type, definition=definition,
                        origin="AI_RECOMMENDED", category=None, confidence=confidence, reasoning=reasoning,
                        ai_suggestion_id=suggestion.id,
                    )
                    result.ai_recommended.append(
                        DetectedRule(
                            rule=rule, column_id=column.id, column_name=column.name, method="AI_RECOMMENDED",
                            confidence=confidence, reasoning=reasoning,
                        )
                    )

        self._db.commit()
        return result
