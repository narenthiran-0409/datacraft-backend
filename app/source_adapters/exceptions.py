class SourceAdapterError(Exception):
    """Base class for source-adapter errors."""


class SourceAuthenticationError(SourceAdapterError):
    """Credentials were rejected by the source database."""


class SourceTimeoutError(SourceAdapterError):
    """The source database did not respond within the bounded timeout."""


class SourceUnreachableError(SourceAdapterError):
    """The source host/port could not be reached (DNS, network, refused)."""


class SourceSSLError(SourceAdapterError):
    """TLS/SSL negotiation with the source database failed."""


class SourceQueryError(SourceAdapterError):
    """The source database rejected or failed to execute a query."""


class SourceDriverNotInstalledError(SourceAdapterError):
    """The optional native vendor driver this provider needs is not
    installed in the current environment."""


class ExactStatsTimeoutError(SourceAdapterError):
    """An exact-stats (COUNT/COUNT DISTINCT) pushdown exceeded
    PROFILING_EXACT_STATS_TIMEOUT_SECONDS. Internal-only: caught inside the
    profiling Celery task, never surfaces as an HTTP error, never fails the
    run — the affected columns fall back to sample-derived statistics."""
