"""Internal helpers shared by scenario models and orchestration."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

if TYPE_CHECKING:
    from .models import ScenarioDefinition, ScenarioPath

_ROW_KEY_FIELD = "syda_scenario_row_key"


def _allocate_path_counts(
    paths: Sequence[ScenarioPath], instance_count: int
) -> Dict[str, int]:
    total_weight = sum(path.weight for path in paths)
    raw = {path.name: instance_count * path.weight / total_weight for path in paths}
    counts = {path.name: int(raw[path.name]) for path in paths}
    remaining = instance_count - sum(counts.values())

    ranked = sorted(
        enumerate(paths),
        key=lambda item: (raw[item[1].name] - int(raw[item[1].name]), -item[0]),
        reverse=True,
    )
    for index in range(remaining):
        counts[ranked[index][1].name] += 1

    if instance_count >= len(paths):
        for path in paths:
            if counts[path.name] > 0:
                continue
            donor = max(paths, key=lambda candidate: counts[candidate.name])
            counts[donor.name] -= 1
            counts[path.name] = 1
    return counts


def _build_assignments(
    paths: Sequence[ScenarioPath], path_counts: Mapping[str, int]
) -> List[ScenarioPath]:
    assignments: List[ScenarioPath] = []
    remaining = dict(path_counts)
    while any(count > 0 for count in remaining.values()):
        for path in paths:
            if remaining[path.name] > 0:
                assignments.append(path)
                remaining[path.name] -= 1
    return assignments


def _foreign_keys(schema: Mapping[str, Any]) -> Dict[str, Tuple[str, str]]:
    result: Dict[str, Tuple[str, str]] = {}
    declared = schema.get("__foreign_keys__", {})
    if isinstance(declared, Mapping):
        for field, target in declared.items():
            parsed = _parse_foreign_key_target(target)
            if parsed:
                result[field] = parsed

    for field, definition in _schema_fields(schema).items():
        if not isinstance(definition, Mapping):
            continue
        references = definition.get("references")
        parsed = _parse_foreign_key_target(references)
        if parsed:
            result.setdefault(field, parsed)
    return result


def _parse_foreign_key_target(target: Any) -> Optional[Tuple[str, str]]:
    if isinstance(target, str) and "." in target:
        table, field = target.split(".", 1)
        return table, field
    if isinstance(target, (list, tuple)) and len(target) == 2:
        return str(target[0]), str(target[1])
    if isinstance(target, Mapping):
        if "schema" in target and "field" in target:
            return str(target["schema"]), str(target["field"])
        nested = target.get("references")
        if nested is not None:
            return _parse_foreign_key_target(nested)
    return None


def _schema_fields(schema: Mapping[str, Any]) -> Dict[str, Any]:
    return {name: value for name, value in schema.items() if not name.startswith("__")}


def _primary_key_fields(schema: Mapping[str, Any]) -> List[str]:
    fields = []
    for name, definition in _schema_fields(schema).items():
        if not isinstance(definition, Mapping):
            continue
        constraints = definition.get("constraints", {})
        if definition.get("primary_key") or (
            isinstance(constraints, Mapping) and constraints.get("primary_key")
        ):
            fields.append(name)
    return fields


def _identity_fields(definition: ScenarioDefinition) -> Dict[str, Set[str]]:
    fields = {
        step.table: set(_primary_key_fields(definition.schemas[step.table]))
        for step in definition.steps
    }
    for step in definition.steps:
        for _, (parent_table, parent_field) in _foreign_keys(
            definition.schemas[step.table]
        ).items():
            if parent_table in fields:
                fields[parent_table].add(parent_field)
    return fields


def _field_type(definition: Any) -> str:
    if isinstance(definition, str):
        return definition.lower()
    if isinstance(definition, Mapping):
        return str(definition.get("type", "text")).lower()
    return "text"


def _planned_identity(
    definition: Any,
    scenario_slug: str,
    table: str,
    field: str,
    row_index: int,
) -> Any:
    field_type = _field_type(definition)
    sequence = row_index + 1
    if field_type in {"integer", "int", "number", "float"}:
        return sequence
    seed = f"{scenario_slug}:{table}:{field}:{sequence}"
    if field_type == "uuid":
        return str(uuid5(NAMESPACE_URL, seed))
    return f"{scenario_slug}-{_slugify(table)}-{_slugify(field)}-{sequence:06d}"


def _split_reference(reference: str) -> Tuple[str, str]:
    if "." not in reference:
        raise ValueError(
            f"Scenario field reference '{reference}' must use 'Table.field'."
        )
    table, field = reference.split(".", 1)
    return table, field


def _validate_field_reference(
    reference: str, schemas: Mapping[str, Mapping[str, Any]]
) -> None:
    table, field = _split_reference(reference)
    if table not in schemas or field not in _schema_fields(schemas[table]):
        raise ValueError(f"Unknown scenario field reference '{reference}'.")


def _slugify(value: str) -> str:
    slug = "".join(
        character.lower() if character.isalnum() else "-" for character in value
    )
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "scenario"


def _scenario_row_keys(
    table: str,
    frame: pd.DataFrame,
    instance_field: str,
) -> List[str]:
    occurrences: Dict[str, int] = {}
    keys: List[str] = []
    table_slug = _slugify(table)
    for instance_id in frame[instance_field]:
        occurrence = occurrences.get(instance_id, 0) + 1
        occurrences[instance_id] = occurrence
        keys.append(f"{instance_id}:{table_slug}:{occurrence:04d}")
    return keys


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(result) if isinstance(result, (bool, type(pd.NA))) else False


def _json_safe(value: Any) -> Any:
    if _is_missing(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def _known_record(
    row: pd.Series,
    instance_field: str,
    path_field: str,
) -> Dict[str, Any]:
    return {
        field: _json_safe(value)
        for field, value in row.items()
        if field not in {instance_field, path_field} and not _is_missing(value)
    }


def _values_equal(left: Any, right: Any) -> bool:
    if _is_missing(left) or _is_missing(right):
        return _is_missing(left) and _is_missing(right)
    try:
        result = left == right
        return bool(result) if not hasattr(result, "all") else bool(result.all())
    except (TypeError, ValueError):
        return False


def _scenario_prompt(
    definition: ScenarioDefinition,
    table: str,
    path_counts: Mapping[str, int],
) -> str:
    summary = definition.description or definition.name
    distribution = ", ".join(f"{path}={count}" for path, count in path_counts.items())
    rules = "; ".join(definition.rules)
    checks = "; ".join(check.name for check in definition.checks)
    metric = ": ".join(
        value for value in (
            definition.secondary_metric.get("label"),
            definition.secondary_metric.get("value"),
        ) if value
    )
    rule_context = f" Business rules: {rules}." if rules else ""
    check_context = f" Executable checks: {checks}." if checks else ""
    metric_context = f" Target metric: {metric}." if metric else ""
    return (
        f"Generate realistic synthetic data for the '{table}' step of the "
        f"'{definition.name}' business scenario. Scenario: {summary}. "
        f"{metric_context}{rule_context}{check_context} "
        f"The deterministic scenario ledger uses these paths: {distribution}. "
        "Focus on realistic descriptive values; structural identifiers, foreign "
        "keys, path overrides, and workflow timestamps are preserved by the ledger. "
        "Executable allowed-values checks further constrain categorical fields."
    )


def _scenario_enrichment_prompt(
    base_prompt: str,
    contexts: Sequence[Mapping[str, Any]],
) -> str:
    serialized_contexts = json.dumps(contexts, ensure_ascii=False, indent=2)
    return (
        f"{base_prompt}\n\n"
        "Generate exactly one output object for each row context below. "
        f"Copy each rowKey exactly into the '{_ROW_KEY_FIELD}' field, use every "
        "rowKey once, and do not invent or omit keys. Generate the remaining fields "
        "so they are consistent with the scenario path, current row, and known "
        "records from earlier workflow steps. Return rows in any order.\n\n"
        f"Row contexts:\n{serialized_contexts}"
    )
