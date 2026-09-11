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


def test_with_timeout_stops_waiting_at_the_deadline_not_after_the_call_finishes() -> None:
    """Regression test: with_timeout previously ran the wrapped call inside
    `with ThreadPoolExecutor(...) as executor:` — Executor.__exit__ calls
    shutdown(wait=True), so even though future.result(timeout=...) raised
    SourceTimeoutError promptly, the wrapper didn't actually return control
    to the caller until the hung call finished. A call hanging far longer
    than the declared timeout would make the caller wait that full duration
    anyway, defeating the point of a timeout. Asserts wall-clock elapsed
    time, not just that an exception is eventually raised, since the old
    behavior did eventually raise — just far too late."""

    @with_timeout(0.1)
    def hangs_much_longer_than_the_timeout() -> str:
        time.sleep(2)
        return "too late"

    started = time.monotonic()
    with pytest.raises(SourceTimeoutError):
        hangs_much_longer_than_the_timeout()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"expected to stop waiting near the 0.1s deadline, took {elapsed:.2f}s"
