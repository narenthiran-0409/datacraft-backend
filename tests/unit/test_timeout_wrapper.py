import time

import pytest

from app.source_adapters.exceptions import SourceTimeoutError
from app.source_adapters.timeout import with_timeout


def test_with_timeout_returns_result_when_fast_enough() -> None:
    @with_timeout(1.0)
    def fast() -> str:
        return "ok"

    assert fast() == "ok"


def test_with_timeout_raises_source_timeout_error_when_slow() -> None:
    @with_timeout(0.05)
    def slow() -> str:
        time.sleep(1)
        return "too late"

    with pytest.raises(SourceTimeoutError):
        slow()


def test_with_timeout_propagates_exceptions_from_the_wrapped_call() -> None:
    @with_timeout(1.0)
    def raises() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        raises()
