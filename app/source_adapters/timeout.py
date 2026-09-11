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
            # Deliberately not a `with ThreadPoolExecutor(...) as executor:`
            # block: Executor.__exit__ calls shutdown(wait=True), which
            # would block this function's return/raise until the submitted
            # call actually finishes — defeating the timeout below for any
            # call that genuinely hangs past `seconds`. shutdown(wait=False)
            # lets us stop waiting exactly when we say we will; the
            # abandoned thread (and whatever driver call it's still running)
            # is left for the interpreter's own atexit executor cleanup.
            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(func, *args, **kwargs)
            try:
                result = future.result(timeout=seconds)
            except FutureTimeoutError:
                executor.shutdown(wait=False)
                raise SourceTimeoutError(f"{func.__qualname__} did not complete within {seconds}s") from None
            executor.shutdown(wait=False)
            return result

        return wrapper

    return decorator
