from app.modules.staging.type_mapping import normalized_type_to_pg_ddl


def test_integer_maps_to_bigint_unconditionally() -> None:
    assert normalized_type_to_pg_ddl("INTEGER") == "BIGINT"
    assert normalized_type_to_pg_ddl("INTEGER", max_length=1, numeric_precision=3, numeric_scale=0) == "BIGINT"


def test_decimal_preserves_precision_and_scale_when_present() -> None:
    assert normalized_type_to_pg_ddl("DECIMAL", numeric_precision=10, numeric_scale=2) == "NUMERIC(10,2)"


def test_decimal_falls_back_to_unbounded_numeric_when_metadata_missing() -> None:
    assert normalized_type_to_pg_ddl("DECIMAL") == "NUMERIC"
    assert normalized_type_to_pg_ddl("DECIMAL", numeric_precision=None, numeric_scale=2) == "NUMERIC"


def test_decimal_falls_back_to_unbounded_numeric_when_precision_out_of_range() -> None:
    assert normalized_type_to_pg_ddl("DECIMAL", numeric_precision=0, numeric_scale=0) == "NUMERIC"
    assert normalized_type_to_pg_ddl("DECIMAL", numeric_precision=5000, numeric_scale=2) == "NUMERIC"
    # scale > precision is nonsensical metadata — conservative fallback.
    assert normalized_type_to_pg_ddl("DECIMAL", numeric_precision=5, numeric_scale=10) == "NUMERIC"


def test_boolean_date_datetime_text() -> None:
    assert normalized_type_to_pg_ddl("BOOLEAN") == "BOOLEAN"
    assert normalized_type_to_pg_ddl("DATE") == "DATE"
    assert normalized_type_to_pg_ddl("DATETIME") == "TIMESTAMP"
    assert normalized_type_to_pg_ddl("TEXT") == "TEXT"


def test_string_preserves_max_length_when_present() -> None:
    assert normalized_type_to_pg_ddl("STRING", max_length=255) == "VARCHAR(255)"


def test_string_falls_back_to_text_when_length_missing_or_unsafe() -> None:
    assert normalized_type_to_pg_ddl("STRING") == "TEXT"
    assert normalized_type_to_pg_ddl("STRING", max_length=0) == "TEXT"
    assert normalized_type_to_pg_ddl("STRING", max_length=99_999_999) == "TEXT"


def test_unrecognized_normalized_type_falls_back_to_text() -> None:
    assert normalized_type_to_pg_ddl("SOMETHING_NEW_NOT_YET_SUPPORTED") == "TEXT"
