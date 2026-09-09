import re
from decimal import Decimal

from app.modules.review.generators import CorrectionGenerator, GeneratorContext, SuggestionDraft, register_generator


@register_generator
class TrimWhitespaceGenerator(CorrectionGenerator):
    fix_type = "trim_whitespace"
    applicable_rule_types = ["PATTERN"]

    def generate(self, ctx: GeneratorContext) -> SuggestionDraft | None:
        failed_value = ctx.validation_failure.failed_value
        if failed_value is None:
            return None

        trimmed = failed_value.strip()
        if trimmed == failed_value:
            return None  # nothing to trim

        pattern = (ctx.rule_version.definition or {}).get("regex")
        if not pattern:
            return None
        try:
            compiled = re.compile(pattern)
        except re.error:
            return None

        # Re-run the ACTUAL regex that rejected this value, against the
        # trimmed value, using the SAME match semantics Phase 5's
        # evaluate_pattern uses (re.match, not re.fullmatch — see
        # app.modules.validation.engine). If trimming doesn't make it pass,
        # whitespace wasn't the cause — don't guess.
        if not compiled.match(trimmed):
            return None

        # Purely mechanical correction — no statistics involved. Highest
        # confidence of the four generators, documented as heuristic policy
        # (not a calibrated probability) per the same discipline as the
        # other three, even though this one is deterministic re-verification.
        confidence = Decimal("0.95")

        return SuggestionDraft(
            suggested_value=trimmed,
            confidence=confidence,
            reasoning="Leading/trailing whitespace removed; trimmed value matches the rule's pattern",
        )
