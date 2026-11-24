"""Evaluate declared field constraints without changing generated data."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from math import isfinite
from typing import Any

import pandas as pd
from pydantic import TypeAdapter, ValidationError

from ..models.scenario import ViolationRecord

MAX_SCHEMA_VIOLATIONS = 1000
_BOOLEAN = TypeAdapter(bool)
_TEMPORAL_TYPES = {"date", "datetime", "timestamp"}
_INTEGER_TYPES = {"integer", "int"}
_NUMERIC_TYPES = _INTEGER_TYPES | {"float", "number"}
_TYPED_TYPES = _NUMERIC_TYPES | _TEMPORAL_TYPES | {"bool", "boolean"}


def numeric_value(value: Any, field_type: str) -> int | float:
    """Canonical numeric value without losing integer precision or accepting bools."""
    if isinstance(value, bool):
        raise ValueError("expected a number, not a boolean")
    if field_type in _INTEGER_TYPES:
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("expected an integer") from None
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("expected a finite integer")
        return int(number)
    number = float(value)
    if not isfinite(number):
        raise ValueError("expected a finite number")
    return number


def _canonical_value(value: Any, field_type: str) -> Any:
    if field_type in _NUMERIC_TYPES:
        return numeric_value(value, field_type)
    if field_type in ("boolean", "bool"):
        return _BOOLEAN.validate_python(value)
    if field_type in _TEMPORAL_TYPES:
        return _bound(value, field_type).isoformat()
    return value


def _comparable_values(values: pd.Series, field_type: str) -> tuple[pd.Series, pd.Series]:
    """Return typed comparison keys and an invalid-value mask, preserving nulls."""
    if field_type not in _TYPED_TYPES:
        return values, pd.Series(False, index=values.index)
    converted, invalid = [], []
    for value, present in zip(values, values.notna()):
        try:
            converted.append(_canonical_value(value, field_type) if present else None)
            invalid.append(False)
        except (ValueError, TypeError, OverflowError):
            converted.append(None)
            invalid.append(True)
    # Object dtype preserves Python integers above 2**53, even alongside nulls.
    return pd.Series(converted, index=values.index, dtype=object), pd.Series(invalid, index=values.index)


def _bound(value: Any, field_type: str) -> Any:
    if field_type in _TEMPORAL_TYPES:
        if not isinstance(value, (str, date, datetime)):
            raise ValueError("expected a date or timestamp")
        result = pd.to_datetime(value, utc=True, errors="raise")
        if pd.isna(result):
            raise ValueError("expected a date or timestamp")
        return result.normalize() if field_type == "date" else result
    if isinstance(value, bool):
        raise ValueError("expected a finite numeric bound")
    if field_type in _INTEGER_TYPES:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("expected a finite numeric bound")
        return result
    result = float(value)
    if not isfinite(result):
        raise ValueError("expected a finite numeric bound")
    return result


def normalize_schema_constraints(schemas: dict[str, Any]) -> dict[str, Any]:
    """Validate before queueing and give generation and evaluation the same values."""
    normalized = deepcopy(schemas)
    for table, schema in normalized.items():
        for column, field in schema.items():
            if column.startswith("__") or not isinstance(field, dict):
                continue
            nested = field.get("constraints")
            if nested is not None and not isinstance(nested, dict):
                raise ValueError(f"{table}.{column}: constraints must be an object.")
            names = (
                "not_null", "nullable", "primary_key", "unique", "enum", "min", "max", "decimals",
            )
            constraints = {key: field[key] for key in names if key in field}
            constraints.update(nested or {})
            field_type = str(field.get("type", "")).lower()
            if constraints.get("decimals") is not None:
                try:
                    precision = numeric_value(constraints["decimals"], "integer")
                    if not 0 <= precision <= 324:
                        raise ValueError("precision out of range")
                except (ValueError, TypeError, OverflowError):
                    raise ValueError(f"{table}.{column}: decimals must be an integer between 0 and 324.") from None
                constraints["decimals"] = precision
            for key in ("not_null", "nullable", "primary_key", "unique"):
                if constraints.get(key) is not None:
                    try:
                        constraints[key] = _BOOLEAN.validate_python(constraints[key])
                    except ValidationError:
                        raise ValueError(
                            f"{table}.{column}: {key} must be a boolean."
                        ) from None
            enum = constraints.get("enum")
            if enum is not None and (
                not isinstance(enum, list)
                or not enum
                or any(
                    value is not None
                    and not isinstance(value, (str, bool, int, float))
                    for value in enum
                )
            ):
                raise ValueError(
                    f"{table}.{column}: enum must be a non-empty list of scalar values."
                )
            if enum is not None and field_type in _TYPED_TYPES:
                try:
                    canonical = [_canonical_value(v, field_type) if v is not None else None for v in enum]
                except (ValueError, TypeError, OverflowError):
                    raise ValueError(f"{table}.{column}: enum values must match the declared field type.") from None
                constraints["enum"] = list(dict.fromkeys(canonical))
            bounds = {}
            for key in ("min", "max"):
                if constraints.get(key) is not None:
                    try:
                        bounds[key] = _bound(
                            constraints[key], field_type
                        )
                    except (ValueError, TypeError, OverflowError, InvalidOperation):
                        raise ValueError(
                            f"{table}.{column}: invalid {key} bound for field type."
                        ) from None
            if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
                raise ValueError(f"{table}.{column}: min must not exceed max.")
            for key, value in bounds.items():
                if field_type in _INTEGER_TYPES:
                    constraints[key] = int(value.to_integral_value(
                        rounding=ROUND_CEILING if key == "min" else ROUND_FLOOR
                    ))
                elif field_type in _TEMPORAL_TYPES:
                    constraints[key] = value.isoformat()
                else:
                    constraints[key] = value
            if (constraints.get("min") is not None and constraints.get("max") is not None
                    and constraints["min"] > constraints["max"]):
                raise ValueError(f"{table}.{column}: bounds contain no available integer values.")
            # Remove stale shorthand so core readers cannot override canonical values.
            for key in names:
                field.pop(key, None)
            if constraints:
                field["constraints"] = constraints
    return normalized


def evaluate_schema_constraints(
    tables: dict[str, pd.DataFrame],
    schemas: dict[str, Any],
) -> tuple[str, int, int, list[ViolationRecord]]:
    """Return compliance, failed cell checks, checked cells, and field diagnostics.

    Nullable values are excluded from membership, uniqueness, and range checks.
    Duplicate checks flag every participating non-null row. A cell can fail more
    than one constraint, so violations count checks rather than distinct records.
    Reference code lists use the existing ``constraints.enum`` representation.
    """
    checked = 0
    failed = 0
    details: list[ViolationRecord] = []
    for table, schema in normalize_schema_constraints(schemas).items():
        frame = tables.get(table)
        # An intentionally absent branch is handled by scenario coverage.
        if frame is None or frame.empty:
            continue
        for column, field in schema.items():
            if column.startswith("__") or not isinstance(field, dict):
                continue
            # Match SchemaLoader: nested declarations override shorthand.
            constraints = {**field, **(field.get("constraints") or {})}
            required = (
                constraints.get("not_null") is True
                or constraints.get("nullable") is False
                or constraints.get("primary_key") is True
            )
            unique = (
                constraints.get("unique") is True
                or constraints.get("primary_key") is True
            )
            active = required or unique or any(
                constraints.get(name) is not None for name in ("enum", "min", "max")
            )
            if not active:
                continue

            def record(mask: pd.Series, rule: str, message: str, count: int) -> None:
                nonlocal checked, failed
                checked += count
                failed += int(mask.sum())
                remaining = MAX_SCHEMA_VIOLATIONS - len(details)
                if remaining <= 0:
                    return
                for position, invalid in enumerate(mask.tolist()):
                    if invalid:
                        # Use the same row identity as causal diagnostics when available.
                        row = frame.iloc[position].get(
                            "scenario_instance_id", str(frame.index[position])
                        )
                        if pd.isna(row):
                            row = str(frame.index[position])
                        details.append(
                            ViolationRecord(
                                table=table,
                                row=str(row),
                                rule=f"schema.{column}.{rule}",
                                error=f"{table}.{column}: {message}",
                            )
                        )
                        remaining -= 1
                        if remaining == 0:
                            break

            if column not in frame:
                record(
                    pd.Series(True, index=frame.index),
                    "missing_column",
                    "Declared constrained field is missing from generated data.",
                    len(frame),
                )
                continue
            values = frame[column]
            present = values.notna()
            field_type = str(field.get("type", "")).lower()
            comparable, invalid_type = _comparable_values(values, field_type)
            if field_type in _TYPED_TYPES and not any(constraints.get(k) is not None for k in ("min", "max")):
                record(invalid_type, "type", "Value is incompatible with the declared field type.", int(present.sum()))
            if required:
                record(~present, "not_null", "Required value is null.", len(frame))
            if constraints.get("enum") is not None:
                record(
                    present & ~invalid_type & ~comparable.isin(constraints["enum"]),
                    "enum",
                    "Value is outside the declared allowed values or reference code list.",
                    int(present.sum()),
                )
            if unique:
                # Nullable unique fields may contain multiple null values.
                duplicates = present & ~invalid_type & comparable.duplicated(keep=False)
                record(
                    duplicates,
                    "unique",
                    "Value is duplicated in a field declared unique.",
                    int(present.sum()),
                )
            for bound, compare in (("min", "lt"), ("max", "gt")):
                if constraints.get(bound) is None:
                    continue
                if field_type in _TEMPORAL_TYPES:
                    temporal_values = values.where(
                        values.map(lambda v: isinstance(v, (str, date, datetime)))
                    )
                    comparable = pd.to_datetime(
                        temporal_values, errors="coerce", utc=True, format="mixed"
                    )
                    if field_type == "date":
                        comparable = comparable.dt.normalize()
                else:
                    if field_type not in _NUMERIC_TYPES:
                        comparable = pd.to_numeric(values, errors="coerce")
                        comparable = comparable.mask(comparable.isin([float("inf"), float("-inf")]))
                invalid = present & (
                    comparable.isna()
                    | getattr(comparable, compare)(_bound(constraints[bound], field_type))
                )
                record(
                    invalid,
                    bound,
                    f"Value must be valid for its field type and satisfy {bound}={constraints[bound]}.",
                    int(present.sum()),
                )
    value = f"{100 * (checked - failed) / checked:.1f}%" if checked else "Not evaluated"
    return value, failed, checked, details
