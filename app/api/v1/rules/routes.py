import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.rules.schemas import (
    RuleAssignmentCreateRequest,
    RuleAssignmentResponse,
    RuleCreateRequest,
    RuleResponse,
    RuleUpdateRequest,
    RuleVersionCreateRequest,
    RuleVersionResponse,
)
from app.modules.rules.service import RuleAssignmentService, RulesService

router = APIRouter(tags=["rules"])


def get_rules_service(db: Session = Depends(get_db)) -> RulesService:
    return RulesService(db)


def get_rule_assignment_service(db: Session = Depends(get_db)) -> RuleAssignmentService:
    return RuleAssignmentService(db)


@router.post("/rules", response_model=RuleResponse, status_code=201)
def create_rule(
    payload: RuleCreateRequest,
    service: RulesService = Depends(get_rules_service),
    current_user: User = Depends(require_permission("rules.manage")),
) -> RuleResponse:
    rule = service.create_rule(
        actor=current_user,
        name=payload.name,
        description=payload.description,
        category=payload.category,
        rule_type=payload.rule_type,
        origin=payload.origin,
        definition=payload.definition,
        severity=payload.severity,
        error_message_template=payload.error_message_template,
    )
    return RuleResponse.model_validate(rule)


@router.get("/rules", response_model=list[RuleResponse])
def list_rules(
    status: str | None = Query(default=None),
    rule_type: str | None = Query(default=None),
    service: RulesService = Depends(get_rules_service),
    _: User = Depends(require_permission("rules.read")),
) -> list[RuleResponse]:
    return [RuleResponse.model_validate(r) for r in service.list_rules(status=status, rule_type=rule_type)]


@router.get("/rules/{rule_id}", response_model=RuleResponse)
def get_rule(
    rule_id: uuid.UUID,
    service: RulesService = Depends(get_rules_service),
    _: User = Depends(require_permission("rules.read")),
) -> RuleResponse:
    return RuleResponse.model_validate(service.get_rule(rule_id))


@router.patch("/rules/{rule_id}", response_model=RuleResponse)
def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdateRequest,
    service: RulesService = Depends(get_rules_service),
    current_user: User = Depends(require_permission("rules.manage")),
) -> RuleResponse:
    rule = service.update_rule(
        actor=current_user,
        rule_id=rule_id,
        description=payload.description,
        category=payload.category,
        status=payload.status,
    )
    return RuleResponse.model_validate(rule)


@router.post("/rules/{rule_id}/versions", response_model=RuleVersionResponse, status_code=201)
def publish_rule_version(
    rule_id: uuid.UUID,
    payload: RuleVersionCreateRequest,
    service: RulesService = Depends(get_rules_service),
    current_user: User = Depends(require_permission("rules.manage")),
) -> RuleVersionResponse:
    version = service.publish_version(
        actor=current_user,
        rule_id=rule_id,
        definition=payload.definition,
        severity=payload.severity,
        error_message_template=payload.error_message_template,
    )
    return RuleVersionResponse.model_validate(version)


@router.get("/rules/{rule_id}/versions", response_model=list[RuleVersionResponse])
def list_rule_versions(
    rule_id: uuid.UUID,
    service: RulesService = Depends(get_rules_service),
    _: User = Depends(require_permission("rules.read")),
) -> list[RuleVersionResponse]:
    return [RuleVersionResponse.model_validate(v) for v in service.list_versions(rule_id)]


@router.post("/rule-assignments", response_model=RuleAssignmentResponse, status_code=201)
def create_rule_assignment(
    payload: RuleAssignmentCreateRequest,
    service: RuleAssignmentService = Depends(get_rule_assignment_service),
    current_user: User = Depends(require_permission("rule_assignments.manage")),
) -> RuleAssignmentResponse:
    assignment = service.create_assignment(
        actor=current_user,
        rule_version_id=payload.rule_version_id,
        dataset_id=payload.dataset_id,
        assignment_scope=payload.assignment_scope,
        column_id=payload.column_id,
        column_ids=payload.column_ids,
        template_id=payload.template_id,
    )
    return RuleAssignmentResponse.model_validate(assignment)


@router.get("/rule-assignments", response_model=list[RuleAssignmentResponse])
def list_rule_assignments(
    dataset_id: uuid.UUID | None = Query(default=None),
    is_enabled: bool | None = Query(default=None),
    service: RuleAssignmentService = Depends(get_rule_assignment_service),
    _: User = Depends(require_permission("rules.read")),
) -> list[RuleAssignmentResponse]:
    return [
        RuleAssignmentResponse.model_validate(a)
        for a in service.list_assignments(dataset_id=dataset_id, is_enabled=is_enabled)
    ]


@router.delete("/rule-assignments/{assignment_id}", response_model=RuleAssignmentResponse)
def disable_rule_assignment(
    assignment_id: uuid.UUID,
    service: RuleAssignmentService = Depends(get_rule_assignment_service),
    current_user: User = Depends(require_permission("rule_assignments.manage")),
) -> RuleAssignmentResponse:
    """Soft-disable only (is_enabled=false) — never a physical delete. See
    RuleAssignmentService.soft_disable for the rationale."""
    assignment = service.soft_disable(actor=current_user, assignment_id=assignment_id)
    return RuleAssignmentResponse.model_validate(assignment)
