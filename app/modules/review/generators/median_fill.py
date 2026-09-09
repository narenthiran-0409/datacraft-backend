from decimal import Decimal

from app.modules.review.generators import CorrectionGenerator, GeneratorContext, SuggestionDraft, register_generator

# Matches app.db.models.metadata.Column.normalized_data_type's numeric values.
_NUMERIC_TYPES = frozenset({"INTEGER", "DECIMAL"})


@register_generator
class MedianFillGenerator(CorrectionGenerator):
    fix_type = "median_fill"
    # Applies to the SAME real Phase 5 rule type as mode_fill (COMPLETENESS —
    # there is no separate "numeric null" rule type in Phase 5). Mutual
    # exclusivity with mode_fill comes from the column's actual
    # normalized_data_type, approved explicitly for this phase.
    applicable_rule_types = ["COMPLETENESS"]

    def generate(self, ctx: GeneratorContext) -> SuggestionDraft | None:
        if ctx.column is None or ctx.column.normalized_data_type not in _NUMERIC_TYPES:
            return None
        if ctx.column_profile is None or ctx.column_profile.median_value is None:
            return None

        stddev = ctx.column_profile.stddev_value
        median = ctx.column_profile.median_value
        if stddev is None:
            return None

        # Confidence heuristic — NOT a calibrated probability. A
        # coefficient-of-variation-style proxy: a small spread (stddev)
        # relative to the median implies the median is more broadly
        # representative of the column's values. Formula:
        # clamp(1 - stddev/|median|, 0.1, 0.9); falls back to a fixed
        # midpoint (0.5) when median == 0, since the ratio is undefined there.
        if median == 0:
            confidence = Decimal("0.5")
        else:
            raw = 1 - (float(stddev) / abs(float(median)))
            confidence = Decimal(str(round(min(0.9, max(0.1, raw)), 3)))

        return SuggestionDraft(
            suggested_value=str(median),
            confidence=confidence,
            reasoning="Median value from the dataset's most recent completed profile run",
        )
