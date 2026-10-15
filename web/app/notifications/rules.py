from __future__ import annotations

import re
from typing import Any


RULE_FIELDS = {
    "source", "source_id", "server", "backend", "type", "host", "service",
    "state", "status_information", "hard", "acknowledged", "downtime",
}
RULE_OPERATORS = {
    "equals", "not_equals", "contains", "not_contains", "starts_with", "ends_with", "wildcard",
}
STATUS_INFORMATION_OPERATORS = {"equals", "not_equals", "contains", "not_contains"}
EVENT_TYPES = {"opened", "state_changed", "recovered", "reminder"}
TEMPLATE_FIELDS = {
    "source", "source_id", "server", "backend", "host", "service", "type",
    "state", "old_state", "timestamp", "event_type", "acknowledged", "downtime",
}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class RuleValidationError(ValueError):
    pass


def validate_conditions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"operator", "conditions"}:
        raise RuleValidationError("conditions must contain only operator and conditions")
    operator = value["operator"]
    conditions = value["conditions"]
    if operator not in {"AND", "OR"}:
        raise RuleValidationError("condition operator must be AND or OR")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 32:
        raise RuleValidationError("rules require between 1 and 32 conditions")
    normalized = []
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"field", "operator", "value"}:
            raise RuleValidationError("each condition must contain field, operator and value")
        field, operation, expected = condition["field"], condition["operator"], condition["value"]
        if field not in RULE_FIELDS:
            raise RuleValidationError(f"unsupported rule field: {field}")
        if operation not in RULE_OPERATORS:
            raise RuleValidationError(f"unsupported rule operator: {operation}")
        if field == "status_information" and operation not in STATUS_INFORMATION_OPERATORS:
            raise RuleValidationError("status_information supports equals, not_equals, contains and not_contains")
        if isinstance(expected, (dict, list)) or expected is None:
            raise RuleValidationError("condition values must be strings or booleans")
        if isinstance(expected, bool):
            if field not in {"hard", "acknowledged", "downtime"} or operation not in {"equals", "not_equals"}:
                raise RuleValidationError("boolean values are supported only for boolean fields and equality")
        elif not isinstance(expected, str) or len(expected) > 256:
            raise RuleValidationError("condition values must be at most 256 characters")
        elif field in {"hard", "acknowledged", "downtime"}:
            raise RuleValidationError("boolean fields require a JSON boolean value")
        normalized.append({"field": field, "operator": operation, "value": expected})
    return {"operator": operator, "conditions": normalized}


def validate_events(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > len(EVENT_TYPES):
        raise RuleValidationError("events must be a non-empty list")
    if any(not isinstance(event, str) or event not in EVENT_TYPES for event in value):
        raise RuleValidationError("events contains an unsupported event type")
    if len(set(value)) != len(value):
        raise RuleValidationError("events must not contain duplicates")
    return value


def validate_template(value: Any, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RuleValidationError(f"template must be non-empty and at most {maximum} characters")
    for placeholder in _PLACEHOLDER.findall(value):
        if placeholder not in TEMPLATE_FIELDS:
            raise RuleValidationError(f"unsupported template variable: {{{placeholder}}}")
    # Reject unmatched braces so templates cannot silently hide misspelled fields.
    remainder = _PLACEHOLDER.sub("", value)
    if "{" in remainder or "}" in remainder:
        raise RuleValidationError("template contains an invalid placeholder")
    return value


def render_template(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in TEMPLATE_FIELDS:
            raise RuleValidationError(f"unsupported template variable: {{{name}}}")
        value = context.get(name)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _PLACEHOLDER.sub(replace, template)


def _matches_one(context: dict[str, Any], condition: dict[str, Any]) -> bool:
    field = condition["field"]
    if field not in context or context[field] is None:
        return False
    actual = context[field]
    expected = condition["value"]
    operation = condition["operator"]
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            return False
        return actual == expected if operation == "equals" else actual != expected
    if isinstance(actual, bool):
        return False
    left = str(actual).casefold()
    right = str(expected).casefold()
    if operation == "equals":
        return left == right
    if operation == "not_equals":
        return left != right
    if operation == "contains":
        return right in left
    if operation == "not_contains":
        return right not in left
    if operation == "starts_with":
        return left.startswith(right)
    if operation == "ends_with":
        return left.endswith(right)
    if operation == "wildcard":
        pattern = re.escape(right).replace(r"\*", ".*").replace(r"\?", ".")
        return re.fullmatch(pattern, left, flags=re.DOTALL) is not None
    return False


def rule_matches(context: dict[str, Any], conditions: dict[str, Any]) -> bool:
    results = [_matches_one(context, condition) for condition in conditions["conditions"]]
    if conditions["operator"] == "AND":
        return all(results)
    return any(results)
