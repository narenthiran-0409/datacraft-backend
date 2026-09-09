from decimal import Decimal

from app.modules.review.generators import CorrectionGenerator, GeneratorContext, SuggestionDraft, register_generator

# Data types treated as categorical/text for mode-fill purposes — matches
# app.db.models.metadata.Column.normalized_data_type's real value set minus
# the two numeric types (which median_fill claims instead).
_CATEGORICAL_TYPES = frozenset({"STRING", "TEXT", "BOOLEAN", "DATE", "DATETIME"})


@register_generator
class ModeFillGenerator(CorrectionGenerator):
    fix_type = "mode_fill"
    # Applies only to COMPLETENESS — the real Phase 5 rule type whose
    # failures always mean "this row's value for this column was NULL"
    # (see app.modules.validation.engine.evaluate_completeness). Shares this
    # rule_type with median_fill; the two are made mutually exclusive by
    # the column's actual normalized_data_type, never both firing for the
    # same issue.
    applicable_rule_types = ["COMPLETENESS"]

    def generate(self, ctx: GeneratorContext) -> SuggestionDraft | None:
        if ctx.column is None or ctx.column.normalized_data_type not in _CATEGORICAL_TYPES:
            return None
        if ctx.column_profile is None or ctx.column_profile.mode_value is None:
            return None

        distinct_pct = ctx.column_profile.distinct_percentage
        if distinct_pct is None:
            return None

        # Confidence heuristic — NOT a calibrated probability. Proxy for how
        # "dominant" a typical value is in the sample: a lower
        # distinct_percentage means values repeat more often on average,
        # which we take as weak evidence the single mode value is broadly
        # representative. Formula: clamp(1 - distinct_percentage/100, 0.1, 0.9).
        # column_profiles does not store the mode's own occurrence count
        # (value_distribution, which would give that directly, is only
        # populated when a profiling run explicitly requested
        # include_top_values=True), so this proxy is used instead because
        # distinct_percentage is unconditionally computed by every profiling
        # run.
        raw = 1 - (float(distinct_pct) / 100)
        confidence = Decimal(str(round(min(0.9, max(0.1, raw)), 3)))

        return SuggestionDraft(
            suggested_value=ctx.column_profile.mode_value,
            confidence=confidence,
            reasoning="Most frequent value (mode) from the dataset's most recent completed profile run",
        )
