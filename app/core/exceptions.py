import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.correlation import get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    code = "APP_ERROR"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, details: dict | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = status.HTTP_404_NOT_FOUND


class InvalidCredentialsError(AppError):
    code = "INVALID_CREDENTIALS"
    status_code = status.HTTP_401_UNAUTHORIZED


class AccountLockedError(AppError):
    code = "ACCOUNT_LOCKED"
    status_code = status.HTTP_403_FORBIDDEN


class InvalidRefreshTokenError(AppError):
    code = "INVALID_REFRESH_TOKEN"
    status_code = status.HTTP_401_UNAUTHORIZED


class UserNotFoundError(NotFoundError):
    code = "USER_NOT_FOUND"


class EmailAlreadyExistsError(AppError):
    code = "EMAIL_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT


class PermissionDeniedError(AppError):
    code = "PERMISSION_DENIED"
    status_code = status.HTTP_403_FORBIDDEN


class DataSourceNotFoundError(NotFoundError):
    code = "DATA_SOURCE_NOT_FOUND"


class DataSourceNameAlreadyExistsError(AppError):
    code = "DATA_SOURCE_NAME_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT


class ConnectionNameAlreadyExistsError(AppError):
    code = "CONNECTION_NAME_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT


class DataSourceHasActiveConnectionsError(AppError):
    code = "DATA_SOURCE_HAS_ACTIVE_CONNECTIONS"
    status_code = status.HTTP_409_CONFLICT


class DataSourceNotActiveError(AppError):
    code = "DATA_SOURCE_NOT_ACTIVE"
    status_code = status.HTTP_409_CONFLICT


class ConnectionNotFoundError(NotFoundError):
    code = "CONNECTION_NOT_FOUND"


class ConnectionTypeNotFoundError(NotFoundError):
    code = "CONNECTION_TYPE_NOT_FOUND"


class CredentialVaultError(AppError):
    code = "CREDENTIAL_VAULT_ERROR"
    status_code = status.HTTP_502_BAD_GATEWAY


class DiscoveryAlreadyRunningError(AppError):
    code = "DISCOVERY_ALREADY_RUNNING"
    status_code = status.HTTP_409_CONFLICT


class DatasetNotFoundError(NotFoundError):
    code = "DATASET_NOT_FOUND"


class InvalidKeyColumnConfigurationError(AppError):
    code = "INVALID_KEY_COLUMN_CONFIGURATION"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class JobNotFoundError(NotFoundError):
    code = "JOB_NOT_FOUND"


class JobNotCancellableError(AppError):
    code = "JOB_NOT_CANCELLABLE"
    status_code = status.HTTP_409_CONFLICT


class ProfilingAlreadyRunningError(AppError):
    code = "PROFILING_ALREADY_RUNNING"
    status_code = status.HTTP_409_CONFLICT


class DatasetNotActiveError(AppError):
    code = "DATASET_NOT_ACTIVE"
    status_code = status.HTTP_409_CONFLICT


class ProfileRunNotFoundError(NotFoundError):
    code = "PROFILE_RUN_NOT_FOUND"


class FullScanExceedsLimitError(AppError):
    code = "FULL_SCAN_EXCEEDS_LIMIT"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class SampleSizeExceedsLimitError(AppError):
    code = "SAMPLE_SIZE_EXCEEDS_LIMIT"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class RuleNotFoundError(NotFoundError):
    code = "RULE_NOT_FOUND"


class RuleVersionNotFoundError(NotFoundError):
    code = "RULE_VERSION_NOT_FOUND"


class UnsupportedRuleTypeError(AppError):
    code = "VALIDATION_UNSUPPORTED_RULE_TYPE"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class InvalidRuleAssignmentScopeError(AppError):
    code = "INVALID_RULE_ASSIGNMENT_SCOPE"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class RuleAssignmentNotFoundError(NotFoundError):
    code = "RULE_ASSIGNMENT_NOT_FOUND"


class DuplicateRuleAssignmentError(AppError):
    code = "DUPLICATE_RULE_ASSIGNMENT"
    status_code = status.HTTP_409_CONFLICT


class InvalidRuleReviewTransitionError(AppError):
    code = "INVALID_RULE_REVIEW_TRANSITION"
    status_code = status.HTTP_409_CONFLICT


class ValidationRunNotFoundError(NotFoundError):
    code = "VALIDATION_RUN_NOT_FOUND"


class ValidationAlreadyRunningError(AppError):
    code = "VALIDATION_ALREADY_RUNNING"
    status_code = status.HTTP_409_CONFLICT


class ReviewRunNotFoundError(NotFoundError):
    code = "REVIEW_RUN_NOT_FOUND"


class SourceValidationRunIncompleteError(AppError):
    code = "SOURCE_VALIDATION_RUN_INCOMPLETE"
    status_code = status.HTTP_409_CONFLICT


class IssueNotFoundError(NotFoundError):
    code = "ISSUE_NOT_FOUND"


class SuggestionNotFoundError(NotFoundError):
    code = "SUGGESTION_NOT_FOUND"


class SuggestionAlreadyDecidedError(AppError):
    code = "SUGGESTION_ALREADY_DECIDED"
    status_code = status.HTTP_409_CONFLICT


class EmptyFinalValueError(AppError):
    code = "EMPTY_FINAL_VALUE"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class InvalidReviewStatusTransitionError(AppError):
    code = "INVALID_REVIEW_STATUS_TRANSITION"
    status_code = status.HTTP_409_CONFLICT


class ReviewRunNotReadyForSubmissionError(AppError):
    code = "REVIEW_RUN_NOT_READY_FOR_SUBMISSION"
    status_code = status.HTTP_409_CONFLICT


class ApprovalRequestAlreadyPendingError(AppError):
    code = "APPROVAL_REQUEST_ALREADY_PENDING"
    status_code = status.HTTP_409_CONFLICT


class NoResolvedIssuesError(AppError):
    code = "NO_RESOLVED_ISSUES"
    status_code = status.HTTP_409_CONFLICT


class ApprovalRequestNotFoundError(NotFoundError):
    code = "APPROVAL_REQUEST_NOT_FOUND"


class IssueNotInApprovalScopeError(AppError):
    code = "ISSUE_NOT_IN_APPROVAL_SCOPE"
    status_code = status.HTTP_409_CONFLICT


class ApprovalNotApprovedError(AppError):
    code = "APPROVAL_NOT_APPROVED"
    status_code = status.HTTP_409_CONFLICT


class NoEligibleIssuesError(AppError):
    code = "NO_ELIGIBLE_ISSUES"
    status_code = status.HTTP_409_CONFLICT


class StagingAlreadyInProgressError(AppError):
    code = "STAGING_ALREADY_IN_PROGRESS"
    status_code = status.HTTP_409_CONFLICT


class ReviewRunArchivedError(AppError):
    code = "REVIEW_RUN_ARCHIVED"
    status_code = status.HTTP_409_CONFLICT


class StagingRunNotFoundError(NotFoundError):
    code = "STAGING_RUN_NOT_FOUND"


class StagingRecordCountExceedsLimitError(AppError):
    code = "STAGING_RECORD_COUNT_EXCEEDS_LIMIT"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class StagingRunNotEligibleError(AppError):
    code = "STAGING_RUN_NOT_ELIGIBLE"
    status_code = status.HTTP_409_CONFLICT


class TargetTypeNotSupportedError(AppError):
    code = "TARGET_TYPE_NOT_SUPPORTED"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class PublishAlreadyInProgressError(AppError):
    code = "PUBLISH_ALREADY_IN_PROGRESS"
    status_code = status.HTTP_409_CONFLICT


class DriftNotAcknowledgedError(AppError):
    code = "DRIFT_NOT_ACKNOWLEDGED"
    status_code = status.HTTP_409_CONFLICT


class InvalidTargetPathError(AppError):
    code = "INVALID_TARGET_PATH"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class TargetAlreadyExistsError(AppError):
    code = "TARGET_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT


class PublishRunNotFoundError(NotFoundError):
    code = "PUBLISH_RUN_NOT_FOUND"


class NoDriftToAcknowledgeError(AppError):
    code = "NO_DRIFT_TO_ACKNOWLEDGE"
    status_code = status.HTTP_409_CONFLICT


class LineageEntityNotFoundError(NotFoundError):
    code = "LINEAGE_ENTITY_NOT_FOUND"


class InvalidDateRangeError(AppError):
    code = "INVALID_DATE_RANGE"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class AIDisabledError(AppError):
    code = "AI_DISABLED"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class AIProviderUnavailableError(AppError):
    code = "AI_PROVIDER_UNAVAILABLE"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE


class AIResponseInvalidError(AppError):
    code = "AI_RESPONSE_INVALID"
    status_code = status.HTTP_502_BAD_GATEWAY


class AIConversationNotFoundError(NotFoundError):
    code = "AI_CONVERSATION_NOT_FOUND"


class AISuggestionNotFoundError(NotFoundError):
    code = "AI_SUGGESTION_NOT_FOUND"


class AIPromptVersionNotFoundError(NotFoundError):
    code = "AI_PROMPT_VERSION_NOT_FOUND"


class PreviewSourceUnavailableError(AppError):
    """The live source connection could not be established or the preview
    query failed (auth rejected, host unreachable, SSL negotiation failed,
    credential vault unreachable, driver not installed, or the query itself
    failed — e.g. the table was renamed/dropped at the source since it was
    last discovered). Mirrors CredentialVaultError's 502: this project's
    convention for "an external dependency we tried to reach failed"."""

    code = "PREVIEW_SOURCE_UNAVAILABLE"
    status_code = status.HTTP_502_BAD_GATEWAY


class PreviewTimeoutError(AppError):
    """The live preview query did not complete within the bounded timeout.
    Kept distinct from PreviewSourceUnavailableError (rather than folded
    into the same bucket) because "too slow right now" and "genuinely
    broken" warrant different frontend retry behavior."""

    code = "PREVIEW_TIMEOUT"
    status_code = status.HTTP_504_GATEWAY_TIMEOUT


def _error_envelope(code: str, message: str, details: dict) -> dict:
    request_id = get_request_id()
    return {
        "error": {
            "code": code,
            "message": message,
            "details": {**details, "request_id": request_id},
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_envelope(
                "VALIDATION_ERROR", "Request validation failed", {"errors": exc.errors()}
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing request")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_envelope(
                "INTERNAL_SERVER_ERROR", "An unexpected error occurred", {}
            ),
        )
