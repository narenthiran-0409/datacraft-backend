import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.exceptions import (
    DuplicateRuleAssignmentError,
    InvalidRuleAssignmentScopeError,
    UnsupportedRuleTypeError,
)
from app.db.models import Column, Connection, Dataset, RuleAssignmentColumn, Schema, User
from app.modules.rules.service import RuleAssignmentService, RulesService


@pytest.fixture
def dataset_with_columns(db: Session, admin_user: User, pg_connection: Connection):
    schema = Schema(connection_id=pg_connection.id, name=f"sch_{uuid.uuid4().hex[:8]}")
    db.add(schema)
    db.flush()
    dataset = Dataset(schema_id=schema.id, name="orders", key_strategy="SINGLE_COLUMN")
    db.add(dataset)
    db.flush()
    col_a = Column(dataset_id=dataset.id, name="id", ordinal_position=0, normalized_data_type="INTEGER")
    col_b = Column(dataset_id=dataset.id, name="email", ordinal_position=1, normalized_data_type="STRING")
    db.add_all([col_a, col_b])
    db.commit()
    db.refresh(dataset)
    db.refresh(col_a)
    db.refresh(col_b)
    return dataset, col_a, col_b


def test_create_rule_rejects_unsupported_rule_type(db: Session, admin_user: User) -> None:
    service = RulesService(db)
    with pytest.raises(UnsupportedRuleTypeError):
        service.create_rule(
            actor=admin_user, name="bad", description=None, category=None, rule_type="REFERENTIAL_INTEGRITY",
            origin="CUSTOM", definition={}, severity="MEDIUM", error_message_template=None,
        )


def test_create_rule_creates_initial_version(db: Session, admin_user: User) -> None:
    service = RulesService(db)
    rule = service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0},
        severity="HIGH", error_message_template=None,
    )
    versions = service.list_versions(rule.id)
    assert len(versions) == 1
    assert versions[0].version_number == 1
    assert versions[0].is_current is True


def test_publish_version_flips_is_current(db: Session, admin_user: User) -> None:
    service = RulesService(db)
    rule = service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="RANGE", origin="CUSTOM", definition={"min": 0, "max": 100}, severity="MEDIUM",
        error_message_template=None,
    )
    v2 = service.publish_version(
        actor=admin_user, rule_id=rule.id, definition={"min": 0, "max": 200}, severity="MEDIUM",
        error_message_template=None,
    )
    versions = {v.version_number: v for v in service.list_versions(rule.id)}
    assert v2.version_number == 2
    assert versions[2].is_current is True
    assert versions[1].is_current is False


def test_single_column_assignment_requires_column_id(db: Session, admin_user: User, dataset_with_columns) -> None:
    dataset, col_a, _ = dataset_with_columns
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={}, severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]

    assignment_service = RuleAssignmentService(db)
    with pytest.raises(InvalidRuleAssignmentScopeError):
        assignment_service.create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=None, column_ids=None, template_id=None,
        )


def test_duplicate_single_column_assignment_rejected(db: Session, admin_user: User, dataset_with_columns) -> None:
    dataset, col_a, _ = dataset_with_columns
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={}, severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    assignment_service = RuleAssignmentService(db)

    assignment_service.create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=col_a.id, column_ids=None, template_id=None,
    )

    with pytest.raises(DuplicateRuleAssignmentError):
        assignment_service.create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col_a.id, column_ids=None, template_id=None,
        )


def test_cross_column_assignment_computes_sorted_key_and_writes_child_rows(
    db: Session, admin_user: User, dataset_with_columns
) -> None:
    dataset, col_a, col_b = dataset_with_columns
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="CROSS_COLUMN", origin="CUSTOM", definition={"check": "all_equal"}, severity="MEDIUM",
        error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    assignment_service = RuleAssignmentService(db)

    # Deliberately pass column_ids in reverse-of-sorted order to prove the
    # stored cross_column_key is sorted, not insertion-order-dependent.
    unsorted_ids = sorted([col_a.id, col_b.id], key=str, reverse=True)
    assignment = assignment_service.create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="CROSS_COLUMN", column_id=None, column_ids=unsorted_ids, template_id=None,
    )

    expected_key = "\x1f".join(sorted(str(c) for c in unsorted_ids))
    assert assignment.cross_column_key == expected_key

    child_rows = db.query(RuleAssignmentColumn).filter(
        RuleAssignmentColumn.rule_assignment_id == assignment.id
    ).all()
    assert {r.column_id for r in child_rows} == {col_a.id, col_b.id}


def test_soft_disable_sets_is_enabled_false_not_a_physical_delete(
    db: Session, admin_user: User, dataset_with_columns
) -> None:
    dataset, col_a, _ = dataset_with_columns
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={}, severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    assignment_service = RuleAssignmentService(db)
    assignment = assignment_service.create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=col_a.id, column_ids=None, template_id=None,
    )

    disabled = assignment_service.soft_disable(actor=admin_user, assignment_id=assignment.id)

    assert disabled.is_enabled is False
    # Still fetchable by id — proves it wasn't physically deleted.
    assert assignment_service.get_assignment(assignment.id).id == assignment.id
