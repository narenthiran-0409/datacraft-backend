from decimal import Decimal

from app.modules.review.generators import CorrectionGenerator, GeneratorContext, SuggestionDraft, register_generator


@register_generator
class RangeClampGenerator(CorrectionGenerator):
    fix_type = "range_clamp"
    applicable_rule_types = ["RANGE"]

    def generate(self, ctx: GeneratorContext) -> SuggestionDraft | None:
        definition = ctx.rule_version.definition or {}
        min_bound = definition.get("min")
        max_bound = definition.get("max")

        try:
            value = float(ctx.validation_failure.failed_value)
        except (TypeError, ValueError):
            return None

        if min_bound is not None and value < float(min_bound):
            clamped = min_bound
        elif max_bound is not None and value > float(max_bound):
            clamped = max_bound
        else:
            # Value doesn't actually violate either configured bound (or
            # neither bound is configured) — not deterministic, don't guess.
            return None

        # Fixed high-confidence heuristic (documented, not calibrated): this
        # is a direct deterministic bounds substitution, not a statistical
        # estimate, so it warrants a high but still-heuristic confidence.
        confidence = Decimal("0.85")

        return SuggestionDraft(
            suggested_value=str(clamped),
            confidence=confidence,
            reasoning=f"Clamped to the rule's configured bound ({clamped})",
        )
