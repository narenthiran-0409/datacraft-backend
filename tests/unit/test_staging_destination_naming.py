import uuid

from app.modules.staging.destination_naming import (
    build_destination_table_name,
    quote_pg_identifier,
    sanitize_identifier_part,
)


def test_sanitize_lowercases_and_replaces_invalid_chars() -> None:
    assert sanitize_identifier_part("DataCraft_AI_Test") == "datacraft_ai_test"
    assert sanitize_identifier_part("My Table Name!") == "my_table_name"
    assert sanitize_identifier_part("weird---chars***here") == "weird_chars_here"


def test_sanitize_never_returns_empty_or_digit_leading() -> None:
    assert sanitize_identifier_part("") == "dataset"
    assert sanitize_identifier_part("!!!") == "dataset"
    assert sanitize_identifier_part("123abc") == "t_123abc"


def test_build_destination_table_name_is_deterministic_and_prefixed() -> None:
    run_id = uuid.uuid4()
    name = build_destination_table_name("DataCraft_AI_Test", run_id)
    assert name == build_destination_table_name("DataCraft_AI_Test", run_id)
    assert name.startswith("datacraft_ai_test__")
    assert run_id.hex[:12] in name


def test_build_destination_table_name_differs_per_run_for_same_dataset() -> None:
    name_a = build_destination_table_name("orders", uuid.uuid4())
    name_b = build_destination_table_name("orders", uuid.uuid4())
    assert name_a != name_b


def test_build_destination_table_name_stays_within_postgres_identifier_limit() -> None:
    long_name = "a" * 200
    name = build_destination_table_name(long_name, uuid.uuid4())
    assert len(name) <= 63


def test_quote_pg_identifier_escapes_internal_quotes() -> None:
    assert quote_pg_identifier("simple") == '"simple"'
    assert quote_pg_identifier('has"quote') == '"has""quote"'
