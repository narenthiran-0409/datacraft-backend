import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import TypeVar

from app.source_adapters.exceptions import SourceTimeoutError

T = TypeVar("T")


def with_timeout(seconds: float) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Enforces a bounded wall-clock timeout around a provider call,
    regardless of whether the underlying driver has its own timeout
    mechanism. Runs the call on a single-use worker thread and raises
    SourceTimeoutError if it doesn't finish in time.

    Note: Python cannot forcibly kill a running thread, so on timeout the
    underlying driver call may continue running in the background after
    this function raises; the caller only stops waiting on it. This is an
    accepted tradeoff for a uniform timeout across five different DB-API
    drivers, several of which have no reliable native query-timeout knob.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func, *args, **kwargs)
                try:
                    return future.result(timeout=seconds)
                except FutureTimeoutError:
                    raise SourceTimeoutError(f"{func.__qualname__} did not complete within {seconds}s") from None

        return wrapper

    return decorator
