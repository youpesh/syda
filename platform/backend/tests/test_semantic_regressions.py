"""Regressions at the boundary between schema validation and generation."""

from datetime import date
import json
import math

import pandas as pd
import pytest

from app.models.scenario import ScenarioConfiguration
from app.services.generation import _LocalTableGenerator, run_generation
from app.services.schema_evaluation import (
    evaluate_schema_constraints,
    normalize_schema_constraints,
)


def test_integer_uniqueness_compares_values_in_the_declared_type():
    frame = pd.DataFrame({"code": ["01", 1]}, dtype=object)
    original = frame.copy(deep=True)
    value, failed, checked, details = evaluate_schema_constraints(
        {"Claim": frame},
        {"Claim": {"code": {"type": "integer", "constraints": {"unique": True}}}},
    )
    assert (value, failed, checked) == ("50.0%", 2, 4)
    assert len(details) == 2
    assert {detail.rule for detail in details} == {"schema.code.unique"}
    pd.testing.assert_frame_equal(frame, original)


def test_integer_reference_values_are_normalized_and_deduplicated():
    schemas = {
        "Claim": {
            "code": {"type": "integer", "constraints": {"enum": ["01", "2.0", 2]}}
        }
    }
    normalized = normalize_schema_constraints(schemas)
    values = normalized["Claim"]["code"]["constraints"]["enum"]
    assert values == [1, 2]
    assert all(type(value) is int for value in values)
    assert schemas["Claim"]["code"]["constraints"]["enum"] == ["01", "2.0", 2]
    result = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"code": [1, "02"]})}, normalized
    )
    assert result[:3] == ("100.0%", 0, 4)


@pytest.mark.parametrize(
    "field_type,values",
    [
        ("boolean", [True, "true"]),
        ("date", ["2026-01-01", date(2026, 1, 1)]),
        ("datetime", ["2026-01-01T05:00:00Z", "2026-01-01T00:00:00-05:00"]),
    ],
)
def test_equivalent_boolean_and_temporal_values_fail_typed_uniqueness(field_type, values):
    result = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"code": values}, dtype=object)},
        {"Claim": {"code": {"type": field_type, "constraints": {"unique": True}}}},
    )
    assert result[1] == 2
    assert len(result[3]) == 2
    assert {detail.rule for detail in result[3]} == {"schema.code.unique"}


@pytest.mark.parametrize(
    "field_type,enum,generated,expected_size",
    [
        ("boolean", [True, "true", False, "false"], ["TRUE", False], 2),
        ("date", ["2026-01-01", "2026-01-01T00:00:00"], [date(2026, 1, 1)], 1),
        ("datetime", ["2026-01-01T05:00:00Z", "2026-01-01T00:00:00-05:00"], ["2026-01-01T00:00:00-05:00"], 1),
    ],
)
def test_boolean_and_temporal_enums_are_canonical_and_json_serializable(
    field_type, enum, generated, expected_size
):
    normalized = normalize_schema_constraints(
        {"Claim": {"code": {"type": field_type, "constraints": {"enum": enum}}}}
    )
    canonical = normalized["Claim"]["code"]["constraints"]["enum"]
    assert len(canonical) == expected_size
    if field_type == "boolean":
        assert canonical == [True, False]
        assert all(type(value) is bool for value in canonical)
    else:
        assert all(isinstance(value, str) for value in canonical)
    json.dumps(normalized)
    result = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"code": generated}, dtype=object)}, normalized
    )
    assert result[0] == "100.0%"
    assert result[1] == 0


@pytest.mark.parametrize(
    "minimum,maximum,expected",
    [
        ("1.0", "2.0", (1, 2)),
        ("1.2", "3.8", (2, 3)),
        ("-3.8", "-1.2", (-3, -2)),
    ],
)
def test_integer_bounds_are_canonical_and_include_only_feasible_integers(
    minimum, maximum, expected
):
    normalized = normalize_schema_constraints(
        {"Claim": {"amount": {"type": "integer", "min": minimum, "max": maximum}}}
    )
    constraints = normalized["Claim"]["amount"]["constraints"]
    assert (constraints["min"], constraints["max"]) == expected
    assert type(constraints["min"]) is int
    assert type(constraints["max"]) is int
    # Exercise the actual consumer that previously crashed on int("1.0").
    result = _LocalTableGenerator().generate_for_schemas(
        normalized, sample_sizes={"Claim": 10}
    )["Claim"]
    assert result["amount"].between(*expected).all()


def test_fractional_integer_bounds_with_no_integer_are_rejected():
    with pytest.raises(ValueError, match=r"Claim.amount"):
        normalize_schema_constraints(
            {"Claim": {"amount": {"type": "integer", "constraints": {"min": "1.2", "max": "1.8"}}}}
        )


def test_integer_bounds_and_uniqueness_keep_precision_above_two_to_the_53():
    lower = 9007199254740993
    schemas = {
        "Claim": {"amount": {"type": "integer", "constraints": {"min": str(lower), "max": str(lower + 2)}}}
    }
    normalized = normalize_schema_constraints(schemas)
    bounds = normalized["Claim"]["amount"]["constraints"]
    assert (bounds["min"], bounds["max"]) == (lower, lower + 2)
    below = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"amount": [lower - 1]}, dtype=object)}, normalized
    )
    assert below[1] == 1
    assert below[3][0].rule == "schema.amount.min"
    unique = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"amount": [lower, lower + 1]}, dtype=object)},
        {"Claim": {"amount": {"type": "integer", "constraints": {"unique": True}}}},
    )
    assert unique[:3] == ("100.0%", 0, 4)


@pytest.mark.parametrize("value", [True, False, 1.5, "1.5", "bad", "NaN", "Infinity"])
def test_invalid_integer_values_cannot_bypass_a_uniqueness_only_check(value):
    result = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"code": [value]}, dtype=object)},
        {"Claim": {"code": {"type": "integer", "constraints": {"unique": True}}}},
    )
    assert result[1] >= 1
    assert any(detail.rule == "schema.code.type" for detail in result[3])


@pytest.mark.parametrize("value,failed", [("9007199254740993", 0), ("9007199254740992", 1)])
def test_nullable_integer_bounds_preserve_precision(value, failed):
    result = evaluate_schema_constraints(
        {"Claim": pd.DataFrame({"code": [value, None]}, dtype=object)},
        {"Claim": {"code": {"type": "integer", "constraints": {"min": "9007199254740993"}}}},
    )
    assert result[1:3] == (failed, 1)


@pytest.mark.parametrize("count", [51, 100])
def test_offline_unique_emails_complete_across_scenario_batches(tmp_path, count):
    scenario = ScenarioConfiguration.model_validate(
        {
            "title": "Unique patients", "description": "Batch boundary regression",
            "recordCount": count, "secondaryMetric": {"label": "Tables", "value": "1"},
            "workflow": ["Patient"], "rules": [],
            "schemas": {"Patient": {
                "id": {"type": "integer", "constraints": {"primary_key": True}},
                "email": {"type": "email", "constraints": {"unique": True}},
            }},
        }
    )
    updates = []
    run_generation(
        "batch-review", scenario, {"id": "offline"}, output_dir=tmp_path,
        update_job=lambda job_id, update: updates.append(update),
    )
    assert updates[-1]["status"] == "complete"
    metric = next(
        metric for metric in updates[-1]["stats"]["evaluationMetrics"]
        if metric["key"] == "semantic_correctness"
    )
    assert metric["status"] == "pass"
    frame = pd.read_csv(next(tmp_path.glob("*.csv")))
    assert len(frame) == count
    assert frame["email"].nunique() == count


def test_unique_integer_state_is_scoped_to_each_table_and_generator():
    def generate(generator, table, count):
        schema = {table: {"code": {"type": "integer", "constraints": {"unique": True, "min": 1, "max": 2}}}}
        return generator.generate_for_schemas(schema, sample_sizes={table: count})[table]

    first = _LocalTableGenerator()
    assert set(generate(first, "A", 2)["code"]) == {1, 2}
    assert set(generate(first, "B", 2)["code"]) == {1, 2}
    with pytest.raises(ValueError, match=r"(?i)(unique|exhaust|available|distinct)"):
        generate(first, "A", 1)
    second = _LocalTableGenerator()
    assert set(generate(second, "A", 2)["code"]) == {1, 2}


def test_unique_enum_pool_exhaustion_does_not_restart_at_batch_boundary():
    generator = _LocalTableGenerator()
    schemas = {"Claim": {"status": {"type": "text", "constraints": {"unique": True, "enum": ["A", "B"]}}}}
    first = generator.generate_for_schemas(schemas, sample_sizes={"Claim": 2})["Claim"]
    assert set(first["status"]) == {"A", "B"}
    with pytest.raises(ValueError, match=r"(?i)(unique|exhaust|available|distinct)"):
        generator.generate_for_schemas(schemas, sample_sizes={"Claim": 1})


def test_failed_local_generation_does_not_consume_values_from_a_later_retry():
    generator = _LocalTableGenerator()
    schemas = {"Claim": {
        "a": {"type": "integer", "constraints": {"unique": True, "min": 1, "max": 2}},
        "b": {"type": "integer", "constraints": {"unique": True, "min": 1, "max": 1}},
    }}
    with pytest.raises(ValueError, match=r"(?i)(unique|exhaust|available|distinct)"):
        generator.generate_for_schemas(schemas, sample_sizes={"Claim": 2})
    schemas["Claim"]["b"]["constraints"]["max"] = 2
    result = generator.generate_for_schemas(schemas, sample_sizes={"Claim": 2})["Claim"]
    assert set(result["a"]) == {1, 2}
    assert set(result["b"]) == {1, 2}


def test_unique_max_only_integer_pool_can_continue_below_zero():
    generator = _LocalTableGenerator()
    schemas = {"Claim": {"code": {"type": "integer", "constraints": {"unique": True, "max": 0}}}}
    first = generator.generate_for_schemas(schemas, sample_sizes={"Claim": 3})["Claim"]
    second = generator.generate_for_schemas(schemas, sample_sizes={"Claim": 3})["Claim"]
    values = first["code"].tolist() + second["code"].tolist()
    assert len(set(values)) == 6
    assert all(value <= 0 for value in values)


def test_unique_float_pool_uses_available_precision_when_decimals_are_unspecified():
    generator = _LocalTableGenerator()
    schemas = {"Claim": {"code": {"type": "float", "constraints": {"unique": True, "min": 0.001, "max": 0.009}}}}
    frame = generator.generate_for_schemas(schemas, sample_sizes={"Claim": 5})["Claim"]
    assert frame["code"].nunique() == 5
    assert frame["code"].between(0.001, 0.009).all()


def test_unique_float_pool_jumps_past_unrepresentable_decimal_grid_ticks():
    generator = _LocalTableGenerator()
    schemas = {"Claim": {"code": {"type": "float", "constraints": {"unique": True, "min": 1e10, "decimals": 15}}}}
    frame = generator.generate_for_schemas(schemas, sample_sizes={"Claim": 2})["Claim"]
    assert frame["code"].nunique() == 2
    assert all(math.isfinite(value) and value >= 1e10 for value in frame["code"])


@pytest.mark.parametrize("precision", ["1.0", 1, "1"])
def test_declared_precision_is_normalized_for_the_generator(precision):
    schemas = {"Claim": {"code": {"type": "float", "constraints": {
        "unique": True, "min": 1, "max": 2, "decimals": precision,
    }}}}
    normalized = normalize_schema_constraints(schemas)
    assert normalized["Claim"]["code"]["constraints"]["decimals"] == 1
    frame = _LocalTableGenerator().generate_for_schemas(schemas, sample_sizes={"Claim": 2})["Claim"]
    assert frame["code"].tolist() == [1.0, 1.1]


@pytest.mark.parametrize("precision", [1.5, True, -1, 325, "bad"])
def test_invalid_precision_is_rejected_before_generation(precision):
    with pytest.raises(ValueError, match=r"Claim.code: decimals"):
        normalize_schema_constraints({"Claim": {"code": {"type": "float", "constraints": {
            "unique": True, "decimals": precision,
        }}}})
