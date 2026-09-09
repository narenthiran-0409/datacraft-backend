"""Pure-logic tests for ProfilingService._resolve_sample_size — both
rejection paths must raise, never silently downgrade."""
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.core.exceptions import FullScanExceedsLimitError, SampleSizeExceedsLimitError
from app.modules.profiling.service import ProfilingService


def _service() -> ProfilingService:
    return ProfilingService(db=MagicMock(), redis_client=MagicMock())


def test_omitted_sample_size_resolves_to_default() -> None:
    dataset = MagicMock(row_count_estimate=1000)
    resolved = _service()._resolve_sample_size(dataset=dataset, sample_size=None, full_scan=False)
    assert resolved == settings.PROFILING_DEFAULT_SAMPLE_SIZE


def test_explicit_sample_size_within_limit_is_used_as_is() -> None:
    dataset = MagicMock(row_count_estimate=1000)
    resolved = _service()._resolve_sample_size(dataset=dataset, sample_size=500, full_scan=False)
    assert resolved == 500


def test_explicit_sample_size_over_limit_raises_without_downgrade() -> None:
    dataset = MagicMock(row_count_estimate=1000)
    oversized = settings.PROFILING_MAX_SAMPLE_SIZE + 1
    with pytest.raises(SampleSizeExceedsLimitError):
        _service()._resolve_sample_size(dataset=dataset, sample_size=oversized, full_scan=False)


def test_full_scan_within_limit_resolves_to_none_for_later_resolution() -> None:
    dataset = MagicMock(row_count_estimate=settings.PROFILING_MAX_FULL_SCAN_ROWS - 1)
    resolved = _service()._resolve_sample_size(dataset=dataset, sample_size=None, full_scan=True)
    assert resolved is None


def test_full_scan_over_limit_raises() -> None:
    dataset = MagicMock(row_count_estimate=settings.PROFILING_MAX_FULL_SCAN_ROWS + 1)
    with pytest.raises(FullScanExceedsLimitError):
        _service()._resolve_sample_size(dataset=dataset, sample_size=None, full_scan=True)
