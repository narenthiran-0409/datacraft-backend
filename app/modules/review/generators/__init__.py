"""Deterministic correction-generator plugin architecture, mirroring Phase
5's rule-evaluation registry-dispatch pattern (app.modules.validation.engine)
exactly: a dict keyed by the thing being dispatched on, each entry a list of
handlers, extensible by adding a new file + decorator registration without
touching dispatch logic.

Every generator in this phase applies to one of the six REAL Phase 5 rule
types (COMPLETENESS, UNIQUENESS, DUPLICATE, RANGE, PATTERN, CROSS_COLUMN) —
confirmed via live inspection of app.modules.rules.service.SUPPORTED_RULE_TYPES
before this module was written. No other rule type is referenced anywhere in
this package.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from app.db.models import Column, ColumnProfile, Issue, RuleVersion, ValidationFailure


@dataclass(frozen=True)
class GeneratorContext:
    issue: Issue
    validation_failure: ValidationFailure
    column: Column | None
    column_profile: ColumnProfile | None
    rule_version: RuleVersion


@dataclass(frozen=True)
class SuggestionDraft:
    suggested_value: str
    confidence: Decimal
    reasoning: str


class CorrectionGenerator(ABC):
    fix_type: ClassVar[str]
    applicable_rule_types: ClassVar[list[str]]

    @abstractmethod
    def generate(self, ctx: GeneratorContext) -> SuggestionDraft | None:
        """Returns a suggestion, or None if this generator's preconditions
        aren't met for this specific issue — None is an expected, valid
        outcome, never an error."""


# rule_type -> list of generator classes applicable to it. More than one
# generator can apply to the same rule_type (mode_fill/median_fill both
# apply to COMPLETENESS, differentiated internally by column data type).
CORRECTION_GENERATOR_REGISTRY: dict[str, list[type[CorrectionGenerator]]] = {}


def register_generator(cls: type[CorrectionGenerator]) -> type[CorrectionGenerator]:
    for rule_type in cls.applicable_rule_types:
        CORRECTION_GENERATOR_REGISTRY.setdefault(rule_type, []).append(cls)
    return cls


# Imported for their registration side-effects only.
from app.modules.review.generators import median_fill as _median_fill  # noqa: E402,F401
from app.modules.review.generators import mode_fill as _mode_fill  # noqa: E402,F401
from app.modules.review.generators import range_clamp as _range_clamp  # noqa: E402,F401
from app.modules.review.generators import trim_whitespace as _trim_whitespace  # noqa: E402,F401
