"""apply_key_columns() queries the DB for every case except the empty-list
guard, so its DB-dependent behavior (single-column/composite/PK-less
upsert-diff correctness, is_primary_key and column_count correctness) is
exercised in tests/integration/test_discovery.py against a real Postgres,
consistent with how Phase 2 handled permission-resolution logic that also
inherently requires a DB. This file covers the one path that's genuinely
independent of the database."""
import pytest

from app.modules.datasets.key_resolution import apply_key_columns


def test_apply_key_columns_rejects_empty_list() -> None:
    with pytest.raises(ValueError):
        apply_key_columns(db=None, dataset=None, column_ids_in_order=[])
