"""Unit tests for Phase 4.3: the pure, unwired cross-column string/template
Evidence Engine (app.modules.ai.evidence_template). Pure-function module —
no database, no fixtures, no mocking, mirrors tests/unit/test_evidence_engine.py
and tests/unit/test_evidence_sequence.py's own isolation and style.
"""
import pytest

from app.modules.ai.evidence_template import discover_template_evidence

# ---------------------------------------------------------------------------
# 1. Name -> email template (the primary example from the spec)
# ---------------------------------------------------------------------------


def test_name_to_email_template_produces_candidate():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "suresh.kumar@example.org"
    assert result.best.strategy == "STRING_TEMPLATE"
    assert result.best.delimiter == "."
    assert result.best.case_transform == "lower"
    assert result.best.suffix == "@example.org"
    assert result.best.contradicting_count == 0
    assert result.best.supporting_count == 3


# ---------------------------------------------------------------------------
# 2. Missing target value — target column absent entirely from the query
# ---------------------------------------------------------------------------


def test_missing_target_value_still_produces_candidate_from_source_alone():
    """The engine never needs the row's own (missing) target value — only
    its source value. Missing is handled identically to invalid (test 3)."""
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Kavitha Rao",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "kavitha.rao@example.org"


# ---------------------------------------------------------------------------
# 3. Invalid target value — same as above, framed explicitly
# ---------------------------------------------------------------------------


def test_invalid_target_value_does_not_prevent_candidate_generation():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    # Suresh's own (invalid) existing value is irrelevant to this function —
    # only his source (name) value is passed in.
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "suresh.kumar@example.org"


# ---------------------------------------------------------------------------
# 4. Whitespace normalization (removal)
# ---------------------------------------------------------------------------


def test_whitespace_removal_template():
    pairs = [
        (("John Smith",), "johnsmith"),
        (("Jane Doe",), "janedoe"),
        (("Bob Lee",), "boblee"),
    ]
    result = discover_template_evidence(
        target_column="username", related_columns=("full_name",), comparable_pairs=pairs, query_source=("Alice Brown",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.delimiter == ""
    assert result.best.candidate_value == "alicebrown"


# ---------------------------------------------------------------------------
# 5. Case normalization
# ---------------------------------------------------------------------------


def test_case_normalization_template():
    pairs = [
        (("John Smith",), "JOHN SMITH"),
        (("Jane Doe",), "JANE DOE"),
        (("Bob Lee",), "BOB LEE"),
    ]
    result = discover_template_evidence(
        target_column="display_name", related_columns=("full_name",), comparable_pairs=pairs,
        query_source=("Alice Brown",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.case_transform == "upper"
    assert result.best.delimiter == " "
    assert result.best.candidate_value == "ALICE BROWN"


# ---------------------------------------------------------------------------
# 6. Delimiter transformation
# ---------------------------------------------------------------------------


def test_delimiter_transformation_template():
    pairs = [
        (("John Smith",), "john_smith"),
        (("Jane Doe",), "jane_doe"),
        (("Bob Lee",), "bob_lee"),
    ]
    result = discover_template_evidence(
        target_column="username", related_columns=("full_name",), comparable_pairs=pairs, query_source=("Alice Brown",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.delimiter == "_"
    assert result.best.candidate_value == "alice_brown"


# ---------------------------------------------------------------------------
# 7. Prefix template
# ---------------------------------------------------------------------------


def test_prefix_template():
    pairs = [
        (("EMP001",), "USER_EMP001"),
        (("EMP002",), "USER_EMP002"),
        (("EMP003",), "USER_EMP003"),
    ]
    result = discover_template_evidence(
        target_column="login", related_columns=("employee_code",), comparable_pairs=pairs, query_source=("EMP004",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.prefix == "USER_"
    assert result.best.suffix == ""
    assert result.best.candidate_value == "USER_EMP004"


# ---------------------------------------------------------------------------
# 8. Suffix template
# ---------------------------------------------------------------------------


def test_suffix_template():
    pairs = [
        (("EMP001",), "EMP001_ACTIVE"),
        (("EMP002",), "EMP002_ACTIVE"),
        (("EMP003",), "EMP003_ACTIVE"),
    ]
    result = discover_template_evidence(
        target_column="status_code", related_columns=("employee_code",), comparable_pairs=pairs,
        query_source=("EMP004",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.prefix == ""
    assert result.best.suffix == "_ACTIVE"
    assert result.best.candidate_value == "EMP004_ACTIVE"


# ---------------------------------------------------------------------------
# 9. Stable domain / template — explicit suffix-stability assertion
# ---------------------------------------------------------------------------


def test_stable_domain_is_learned_and_reused():
    pairs = [
        (("Arun Kumar",), "arun.kumar@mycompany.io"),
        (("Priya Raj",), "priya.raj@mycompany.io"),
        (("Vijay Kumar",), "vijay.kumar@mycompany.io"),
        (("Meena Devi",), "meena.devi@mycompany.io"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.suffix == "@mycompany.io"
    assert result.best.candidate_value == "suresh.kumar@mycompany.io"


# ---------------------------------------------------------------------------
# 10. Multiple supporting pairs
# ---------------------------------------------------------------------------


def test_multiple_supporting_pairs_increase_supporting_count():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
        (("Meena Devi",), "meena.devi@example.org"),
        (("David Wilson",), "david.wilson@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.supporting_count == 5
    # confidence = min(1.0, observation_count / (min_supporting_pairs * 2))
    # = min(1.0, 5 / 6) — same size-discount spirit as evidence_sequence.py;
    # confidence only reaches 1.0 once observations are at least 2x the floor.
    assert result.best.confidence == pytest.approx(5 / 6)


# ---------------------------------------------------------------------------
# 11. One pair only -> insufficient
# ---------------------------------------------------------------------------


def test_single_pair_is_insufficient():
    pairs = [(("John",), "john@example.com")]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Jane",)
    )
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 12. Contradictory pairs -> ambiguous
# ---------------------------------------------------------------------------


def test_contradictory_pairs_is_ambiguous():
    pairs = [
        (("Arun",), "arun@example.com"),
        (("Priya",), "priya@company.com"),
        (("Vijay",), "vj@example.net"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh",)
    )
    assert result.status == "AMBIGUOUS"
    assert result.best is None


# ---------------------------------------------------------------------------
# 13. Competing domains -> ambiguous
# ---------------------------------------------------------------------------


def test_competing_domains_is_ambiguous_not_majority_vote():
    """Two people share a gmail.com domain, a third has yahoo.com — the
    engine must NOT silently pick the majority domain."""
    pairs = [
        (("Arun Kumar",), "arun.kumar@gmail.com"),
        (("Priya Raj",), "priya.raj@gmail.com"),
        (("Vijay Kumar",), "vijay.kumar@yahoo.com"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "AMBIGUOUS"
    assert result.best is None


# ---------------------------------------------------------------------------
# 14. Unrelated strings -> no relationship
# ---------------------------------------------------------------------------


def test_unrelated_strings_is_no_relationship():
    pairs = [
        (("Arun Kumar",), "banana"),
        (("Priya Raj",), "apple"),
        (("Vijay Kumar",), "cherry"),
    ]
    result = discover_template_evidence(
        target_column="fruit", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 15. Empty source
# ---------------------------------------------------------------------------


def test_empty_source_pair_is_excluded_without_crashing():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
        (("",), "unrelated@example.org"),  # empty source — must be silently excluded
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.observed_pairs_count == 3  # the empty-source pair did not count
    assert result.best.candidate_value == "suresh.kumar@example.org"


# ---------------------------------------------------------------------------
# 16. Null source
# ---------------------------------------------------------------------------


def test_null_source_pair_is_excluded_without_crashing():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
        ((None,), "unrelated@example.org"),  # null source — must be silently excluded
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.observed_pairs_count == 3
    assert result.best.candidate_value == "suresh.kumar@example.org"


# ---------------------------------------------------------------------------
# 17. Duplicate source values
# ---------------------------------------------------------------------------


def test_duplicate_source_values_do_not_break_counting():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Arun Kumar",), "arun.kumar@example.org"),  # exact duplicate row
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.observed_pairs_count == 4
    assert result.best.supporting_count == 4
    assert result.best.candidate_value == "suresh.kumar@example.org"


# ---------------------------------------------------------------------------
# 18. Duplicate target values
# ---------------------------------------------------------------------------


def test_duplicate_target_values_do_not_crash():
    """Two different sources incorrectly sharing one target value (a data
    quality problem in its own right) must not crash the engine — it
    simply contradicts whatever hypothesis would have otherwise fit both
    sources uniquely, correctly weakening confidence rather than erroring."""
    pairs = [
        (("Arun Kumar",), "shared@example.org"),
        (("Priya Raj",), "shared@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    # No crash; and since "shared@example.org" cannot be explained by any
    # per-source template, no hypothesis achieves zero contradictions.
    assert result.status in {"AMBIGUOUS", "NO_RELATIONSHIP"}
    assert result.best is None


# ---------------------------------------------------------------------------
# 19. Multiple reference columns
# ---------------------------------------------------------------------------


def test_multiple_reference_columns_first_and_last_name():
    pairs = [
        (("Arun", "Kumar"), "arun.kumar@example.org"),
        (("Priya", "Raj"), "priya.raj@example.org"),
        (("Vijay", "Kumar"), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("first_name", "last_name"), comparable_pairs=pairs,
        query_source=("Suresh", "Kumar"),
    )
    assert result.status == "CANDIDATE"
    assert result.related_columns == ("first_name", "last_name")
    assert result.best.candidate_value == "suresh.kumar@example.org"


# ---------------------------------------------------------------------------
# 20. Arbitrary column names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_column,related_columns",
    [
        ("col_a", ("col_b",)),
        ("contact_value", ("full_label",)),
        ("attr_7", ("attr_3",)),
    ],
)
def test_arbitrary_column_names_behave_identically(target_column, related_columns):
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column=target_column, related_columns=related_columns, comparable_pairs=pairs,
        query_source=("Suresh Kumar",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "suresh.kumar@example.org"
    assert result.target_column == target_column
    assert result.related_columns == related_columns


# ---------------------------------------------------------------------------
# 21. No hardcoded domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["@gmail.com", "@example.org", "@mycompany.io", "@sensor.local", "@x.co"])
def test_no_hardcoded_domain_any_stable_domain_is_learned(domain):
    pairs = [
        (("Arun Kumar",), f"arun.kumar{domain}"),
        (("Priya Raj",), f"priya.raj{domain}"),
        (("Vijay Kumar",), f"vijay.kumar{domain}"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.suffix == domain
    assert result.best.candidate_value == f"suresh.kumar{domain}"


# ---------------------------------------------------------------------------
# 22. No hardcoded business names — clearly non-human-name tokens
# ---------------------------------------------------------------------------


def test_no_hardcoded_business_names_works_for_non_name_tokens():
    pairs = [
        (("Widget Alpha",), "widget.alpha@sensor.local"),
        (("Widget Beta",), "widget.beta@sensor.local"),
        (("Widget Gamma",), "widget.gamma@sensor.local"),
    ]
    result = discover_template_evidence(
        target_column="endpoint", related_columns=("device_label",), comparable_pairs=pairs,
        query_source=("Widget Delta",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "widget.delta@sensor.local"


# ---------------------------------------------------------------------------
# 23. Malformed target among otherwise-clean data
# ---------------------------------------------------------------------------


def test_malformed_target_among_clean_pairs_prevents_confident_candidate():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
        (("Meena Devi",), "???not-an-email-at-all???"),  # malformed — contradicts the otherwise-clean template
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",)
    )
    assert result.status == "AMBIGUOUS"
    assert result.best is None


# ---------------------------------------------------------------------------
# 24. Target already correct — asking about a row that isn't actually broken
# ---------------------------------------------------------------------------


def test_querying_a_source_whose_target_is_already_correct_is_consistent():
    """The learned transformation is a stable function of the source value
    alone — asking for a candidate for a row that already has the correct
    value must reproduce that same value, not something different."""
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Arun Kumar",)
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "arun.kumar@example.org"


# ---------------------------------------------------------------------------
# 25. Candidate generation for a previously unseen source value
# ---------------------------------------------------------------------------


def test_candidate_for_previously_unseen_source_value():
    pairs = [
        (("Arun Kumar",), "arun.kumar@example.org"),
        (("Priya Raj",), "priya.raj@example.org"),
        (("Vijay Kumar",), "vijay.kumar@example.org"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs,
        query_source=("Completely Unseen Name",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "completely.unseen.name@example.org"


# ---------------------------------------------------------------------------
# IMPORTANT NEGATIVE TEST — no AI/common-knowledge fallback
# ---------------------------------------------------------------------------


def test_no_candidate_without_real_observed_evidence():
    """Without ANY training pairs at all, the engine must never produce
    "suresh.kumar@gmail.com" or any other value from common internet
    conventions/LLM-style knowledge — a candidate can only ever originate
    from actually observed dataset evidence."""
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=[], query_source=("Suresh Kumar",)
    )
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None


def test_insufficient_pairs_below_floor_never_guesses_common_convention():
    """Two pairs is still below the observation floor — even though both
    happen to agree, that alone is not enough independent support."""
    pairs = [
        (("Arun Kumar",), "arun.kumar@gmail.com"),
        (("Priya Raj",), "priya.raj@gmail.com"),
    ]
    result = discover_template_evidence(
        target_column="email", related_columns=("name",), comparable_pairs=pairs, query_source=("Suresh Kumar",),
        min_observations=3,
    )
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None


# ---------------------------------------------------------------------------
# Customer_Orders — TEST SCENARIO ONLY, not a production assumption
# ---------------------------------------------------------------------------


def _customer_orders_training_pairs():
    """The real, clean (name -> email) pairs from the live Customer_Orders
    dataset investigated in the Phase 3 forensic report — used here purely
    as a realistic TEST SCENARIO. discover_template_evidence() itself has
    no knowledge of "Customer_Orders", "name", or "email"; nothing in
    evidence_template.py reads target_column/related_columns for any
    decision."""
    return [
        (("Arun Kumar",), "arun.kumar@gmail.com"),
        (("Priya Raj",), "priya.raj@gmail.com"),
        (("John Smith",), "john.smith@gmail.com"),
        (("Meena Devi",), "meena.devi@gmail.com"),
        (("David Wilson",), "david.wilson@gmail.com"),
        (("Vijay Kumar",), "vijay.kumar@gmail.com"),
        (("Anitha Raj",), "anitha.raj@gmail.com"),
        (("Ramesh Babu",), "ramesh.babu@gmail.com"),
        (("Lakshmi",), "lakshmi@gmail.com"),  # single-token name — still compatible
        (("Peter John",), "peter.john@gmail.com"),
    ]


def test_customer_orders_scenario_suresh_invalid_email():
    result = discover_template_evidence(
        target_column="email", related_columns=("customer_name",),
        comparable_pairs=_customer_orders_training_pairs(), query_source=("Suresh Kumar",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "suresh.kumar@gmail.com"
    assert result.best.contradicting_count == 0
    assert result.best.supporting_count == 10


def test_customer_orders_scenario_kavitha_missing_email():
    result = discover_template_evidence(
        target_column="email", related_columns=("customer_name",),
        comparable_pairs=_customer_orders_training_pairs(), query_source=("Kavitha Rao",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "kavitha.rao@gmail.com"


def test_customer_orders_scenario_sara_invalid_email():
    result = discover_template_evidence(
        target_column="email", related_columns=("customer_name",),
        comparable_pairs=_customer_orders_training_pairs(), query_source=("Sara Thomas",),
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "sara.thomas@gmail.com"
