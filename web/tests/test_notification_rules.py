"""Observable matching semantics for fields present in the Desktop contract."""

import pytest

from app.notifications.rules import RuleValidationError, rule_matches, validate_conditions


CONTEXT = {
    "source": "NaftaMon", "source_id": 7, "server": "Thruk Norte",
    "backend": "prod-checks", "type": "service", "host": "DB-Primary-01",
    "service": "Disk /", "state": "CRITICAL", "hard": True,
    "acknowledged": False, "downtime": False,
    "status_information": "Connection refused — DISK CRITICAL",
}


@pytest.mark.parametrize("field,operator,value,expected", [
    ("host", "equals", "db-primary-01", True),
    ("host", "not_equals", "db-primary-02", True),
    ("host", "starts_with", "DB-", True),
    ("host", "ends_with", "-01", True),
    ("host", "wildcard", "db-*-??", True),
    ("service", "contains", "disk", True),
    ("service", "not_contains", "memory", True),
    ("server", "contains", "NORTE", True),
    ("backend", "equals", "prod-checks", True),
    ("source", "equals", "naftamon", True),
    ("type", "equals", "service", True),
    ("state", "equals", "CRITICAL", True),
    ("state", "equals", "WARNING", False),
    ("state", "equals", "UNKNOWN", False),
    ("status_information", "contains", "connection REFUSED", True),
    ("status_information", "not_contains", "maintenance", True),
    ("status_information", "equals", "connection refused — disk critical", True),
    ("status_information", "not_equals", "connection refused", True),
    ("acknowledged", "equals", False, True),
    ("downtime", "equals", True, False),
    ("hard", "equals", True, True),
])
def test_supported_conditions(field, operator, value, expected):
    conditions = validate_conditions({
        "operator": "AND", "conditions": [{"field": field, "operator": operator, "value": value}],
    })
    assert rule_matches(CONTEXT, conditions) is expected


def test_and_or_combinations_and_absent_field():
    conditions = [
        {"field": "host", "operator": "wildcard", "value": "db-*"},
        {"field": "state", "operator": "equals", "value": "WARNING"},
    ]
    assert rule_matches(CONTEXT, validate_conditions({"operator": "AND", "conditions": conditions})) is False
    assert rule_matches(CONTEXT, validate_conditions({"operator": "OR", "conditions": conditions})) is True
    partial = dict(CONTEXT)
    partial.pop("service")
    missing = validate_conditions({"operator": "AND", "conditions": [
        {"field": "service", "operator": "not_equals", "value": "disk"},
    ]})
    assert rule_matches(partial, missing) is False


@pytest.mark.parametrize("operation", ["contains", "not_contains", "equals", "not_equals"])
def test_status_information_missing_never_matches_including_negative_operators(operation):
    context = dict(CONTEXT)
    context.pop("status_information")
    conditions = validate_conditions({"operator": "AND", "conditions": [
        {"field": "status_information", "operator": operation, "value": "maintenance"},
    ]})
    assert rule_matches(context, conditions) is False


def test_status_information_combines_with_state_host_and_service_using_and_or():
    and_conditions = validate_conditions({"operator": "AND", "conditions": [
        {"field": "state", "operator": "equals", "value": "critical"},
        {"field": "host", "operator": "equals", "value": "db-primary-01"},
        {"field": "service", "operator": "equals", "value": "disk /"},
        {"field": "status_information", "operator": "contains", "value": "disk critical"},
    ]})
    assert rule_matches(CONTEXT, and_conditions) is True
    warning = dict(CONTEXT, state="WARNING")
    assert rule_matches(warning, and_conditions) is False

    or_conditions = validate_conditions({"operator": "OR", "conditions": [
        {"field": "state", "operator": "equals", "value": "WARNING"},
        {"field": "status_information", "operator": "contains", "value": "disk critical"},
    ]})
    assert rule_matches(CONTEXT, or_conditions) is True


@pytest.mark.parametrize("operation", ["starts_with", "ends_with", "wildcard"])
def test_status_information_rejects_operators_outside_requested_set(operation):
    with pytest.raises(RuleValidationError):
        validate_conditions({"operator": "AND", "conditions": [
            {"field": "status_information", "operator": operation, "value": "disk*"},
        ]})


def test_plugin_output_is_not_a_rule_field():
    with pytest.raises(RuleValidationError):
        validate_conditions({"operator": "AND", "conditions": [
            {"field": "output", "operator": "contains", "value": "space"},
        ]})
