import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RuleCreateRequest(BaseModel):
    name: str
    description: str | None = None
    category: str | None = None
    rule_type: str = Field(
        description="One of the six Phase 5 rule types: COMPLETENESS, UNIQUENESS, DUPLICATE, "
        "RANGE, PATTERN, CROSS_COLUMN. Any other value is rejected with 422 — "
        "REFERENTIAL_INTEGRITY is explicitly deferred to a future phase."
    )
    origin: str = "BUILT_IN"
    definition: dict = Field(description="rule_versions.definition for the initial version (version_number=1).")
    severity: str = "MEDIUM"
    error_message_template: str | None = None


class RuleUpdateRequest(BaseModel):
    description: str | None = None
    category: str | None = None
    status: str | None = None


class RuleResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: str | None
    rule_type: str
    origin: str
    status: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class RuleVersionCreateRequest(BaseModel):
    definition: dict
    severity: str = "MEDIUM"
    error_message_template: str | None = None


class RuleVersionResponse(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    version_number: int
    definition: dict
    severity: str
    error_message_template: str | None
    is_current: bool
    created_by: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RuleAssignmentCreateRequest(BaseModel):
    rule_version_id: uuid.UUID
    dataset_id: uuid.UUID
    assignment_scope: str = Field(description="SINGLE_COLUMN, DATASET_LEVEL, or CROSS_COLUMN")
    column_id: uuid.UUID | None = Field(
        default=None, description="Required (and only meaningful) when assignment_scope=SINGLE_COLUMN"
    )
    column_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Required (and only meaningful) when assignment_scope=CROSS_COLUMN — the ordered "
        "set of columns the rule spans. cross_column_key is derived from this, sorted, by the server.",
    )
    template_id: uuid.UUID | None = None


class RuleAssignmentResponse(BaseModel):
    id: uuid.UUID
    rule_version_id: uuid.UUID
    dataset_id: uuid.UUID
    assignment_scope: str
    column_id: uuid.UUID | None
    cross_column_key: str | None
    template_id: uuid.UUID | None
    is_enabled: bool
    paused_at: datetime | None
    assigned_by: uuid.UUID | None
    assigned_at: datetime
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}
