"""Schema quality gates use declared constraints and report actionable failures."""

import pandas as pd
import pytest

from app.models.scenario import ScenarioConfiguration
from app.services.generation import _evaluation_metrics
from app.services.schema_evaluation import evaluate_schema_constraints
from app.services.schema_evaluation import MAX_SCHEMA_VIOLATIONS, normalize_schema_constraints
from app.services import generation


def test_healthcare_reference_codes_and_schema_failures_reach_quality_gate():
    schema = {
        "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
        "status": {"type": "text", "constraints": {"not_null": True, "enum": ["APPROVED", "DENIED", "PENDING_REVIEW"]}},
        # A deliberately bounded reference fixture, not a full medical code catalog.
        "diagnosis": {"type": "text", "constraints": {"enum": ["E11.9", "I10"]}},
        "amount": {"type": "float", "constraints": {"min": 0, "max": 1000}},
    }
    scenario = ScenarioConfiguration.model_validate({
        "title": "Healthcare schema validation", "description": "Claims quality gate",
        "recordCount": 2, "secondaryMetric": {"label": "Tables", "value": "1"},
        "workflow": ["Claim"], "rules": [], "schemas": {"Claim": schema},
    })
    frame = pd.DataFrame({
        "claim_id": [1, 2], "status": ["APPROVED", "DENIED"],
        "diagnosis": ["E11.9", "I10"], "amount": [25, 0],
        "scenario_instance_id": ["c1", "c2"], "scenario_path": ["default", "default"],
    })
    original = frame.copy(deep=True)
    metrics, result, _, details = _evaluation_metrics(
        {"Claim": frame}, scenario.schemas, scenario, return_details=True,
    )
    assert next(m for m in metrics if m.key == "semantic_correctness").status == "pass"
    assert result == "partial"
    assert details == []
    pd.testing.assert_frame_equal(frame, original)

    frame["amount"] = frame["amount"].astype(object)
    frame.loc[1, ["claim_id", "status", "diagnosis", "amount"]] = [1, None, "INVALID", "bad"]
    metrics, result, _, details = _evaluation_metrics(
        {"Claim": frame}, scenario.schemas, scenario, return_details=True,
    )
    metric = next(m for m in metrics if m.key == "semantic_correctness")
    assert result == "fail"
    assert metric.status == "fail"
    assert metric.violations == len(details) == 6
    assert {d.rule for d in details} == {
        "schema.claim_id.unique", "schema.status.not_null", "schema.diagnosis.enum",
        "schema.amount.min", "schema.amount.max",
    }
    assert all(d.table == "Claim" and "Claim." in d.error for d in details)
    assert {d.row for d in details if d.rule == "schema.claim_id.unique"} == {"c1", "c2"}


@pytest.mark.parametrize("constraint", [{"not_null": True}, {"nullable": False}, {"primary_key": True}])
def test_required_constraint_spellings_detect_nulls(constraint):
    value, failed, checked, details = evaluate_schema_constraints(
        {"T": pd.DataFrame({"id": [None]})},
        {"T": {"id": {"type": "integer", "constraints": constraint}}},
    )
    assert value == "0.0%"
    assert failed == checked == 1
    assert details[0].rule == "schema.id.not_null"


def test_nullable_membership_and_uniqueness_ignore_nulls_with_duplicate_indexes():
    frame = pd.DataFrame({"code": [None, pd.NA, "I10", "E11.9"]}, index=[0, 0, 1, 1])
    result = evaluate_schema_constraints({"T": frame}, {"T": {
        "code": {"type": "text", "constraints": {"nullable": True, "unique": True, "enum": ["I10", "E11.9"]}},
    }})
    assert result == ("100.0%", 0, 4, [])


def test_missing_constrained_column_fails_but_empty_branch_is_not_evaluated():
    schemas = {"T": {"code": {"type": "text", "constraints": {"enum": ["I10"]}}}}
    result = evaluate_schema_constraints({"T": pd.DataFrame({"other": [1]})}, schemas)
    assert result[1:3] == (1, 1)
    assert result[3][0].rule == "schema.code.missing_column"
    assert evaluate_schema_constraints({"T": pd.DataFrame()}, schemas) == ("Not evaluated", 0, 0, [])


def test_shorthand_and_nested_constraints_and_numeric_bounds():
    frame = pd.DataFrame({"amount": [-1, 0, 10, 11, "bad", None]})
    value, failed, checked, details = evaluate_schema_constraints({"T": frame}, {"T": {
        "amount": {"type": "number", "min": 2, "constraints": {"min": 0, "max": 10}},
    }})
    assert (value, failed, checked) == ("60.0%", 4, 10)
    assert len(details) == 4


@pytest.mark.parametrize("value", [True, "true", "TRUE", "yes", 1])
def test_boolean_representations_are_enforced_and_normalized(value):
    schema = {"T": {"code": {"type": "text", "constraints": {"unique": value, "not_null": value}}}}
    original = {"T": {"code": {"type": "text", "constraints": {"unique": value, "not_null": value}}}}
    normalized = normalize_schema_constraints(schema)
    assert normalized["T"]["code"]["constraints"]["unique"] is True
    assert schema == original
    result = evaluate_schema_constraints({"T": pd.DataFrame({"code": [None, "x", "x"]})}, schema)
    assert result[1] == 3


@pytest.mark.parametrize("field_type", ["date", "datetime", "timestamp"])
def test_temporal_bounds_handle_invalid_values_nulls_and_inclusive_endpoints(field_type):
    frame = pd.DataFrame({"day": ["2026-01-01", "2026-01-31", "2025-12-31", "bad", None]})
    result = evaluate_schema_constraints({"T": frame}, {"T": {
        "day": {"type": field_type, "constraints": {"min": "2026-01-01", "max": "2026-01-31"}},
    }})
    assert result[:3] == ("62.5%", 3, 8)


def test_datetime_bounds_compare_equivalent_timezones():
    result = evaluate_schema_constraints({"T": pd.DataFrame({"day": ["2026-01-01T05:00:00Z"]})}, {"T": {
        "day": {"type": "datetime", "constraints": {"min": "2026-01-01T00:00:00-05:00", "max": "2026-01-01T00:00:00-05:00"}},
    }})
    assert result[:3] == ("100.0%", 0, 2)


@pytest.mark.parametrize("constraints", [
    {"enum": "APPROVED"}, {"enum": 7}, {"enum": []}, {"enum": [{"code": "I10"}]},
    {"unique": "sometimes"}, {"min": "bad"}, {"min": 10, "max": 0},
])
def test_invalid_constraints_are_rejected_with_field_context(constraints):
    with pytest.raises(ValueError, match=r"Claim.code:"):
        normalize_schema_constraints({"Claim": {"code": {"type": "number", "constraints": constraints}}})


def test_schema_diagnostic_cap_keeps_exact_totals_and_reports_omissions():
    count = MAX_SCHEMA_VIOLATIONS + 20
    schema = {"T": {"code": {"type": "text", "constraints": {"unique": True}}}}
    frame = pd.DataFrame({"code": ["duplicate"] * count})
    result = evaluate_schema_constraints({"T": frame}, schema)
    assert result[:3] == ("0.0%", count, count)
    assert len(result[3]) == MAX_SCHEMA_VIOLATIONS
    scenario = ScenarioConfiguration.model_validate({
        "title": "Capped diagnostics", "description": "Counts stay exact", "recordCount": count,
        "secondaryMetric": {"label": "Tables", "value": "1"}, "workflow": ["T"], "rules": [], "schemas": schema,
    })
    metrics, status, violations, details = _evaluation_metrics({"T": frame}, schema, scenario, return_details=True)
    semantic = next(m for m in metrics if m.key == "semantic_correctness")
    assert semantic.violations == violations == count
    assert status == "fail"
    assert len(details) == MAX_SCHEMA_VIOLATIONS
    assert semantic.model_dump(by_alias=True)["omittedViolations"] == 20
    assert "20 additional schema violation details omitted" in semantic.detail


@pytest.mark.parametrize("code,expected_status", [("I10", "complete"), ("INVALID", "failed")])
def test_generation_withholds_files_when_reference_code_check_fails(monkeypatch, tmp_path, code, expected_status):
    scenario = ScenarioConfiguration.model_validate({
        "title": "Diagnosis codes", "description": "Reference list quality gate",
        "recordCount": 1, "secondaryMetric": {"label": "Tables", "value": "1"},
        "workflow": ["Encounter"], "rules": [],
        "schemas": {"Encounter": {
            "id": {"type": "integer", "constraints": {"primary_key": True}},
            "code": {"type": "text", "constraints": {"enum": ["I10", "E11.9"]}},
        }},
    })
    # Inject a provider response; keep real scenario planning and the final gate.
    def generate_fields(self, schemas, sample_sizes=None, **kwargs):
        return {name: pd.DataFrame({
            field: ([code] * sample_sizes[name] if field == "code" else
                    definition.get("constraints", {}).get("enum", [1] * sample_sizes[name]))
            for field, definition in schema.items() if not field.startswith("__")
        }) for name, schema in schemas.items()}

    monkeypatch.setattr(generation._LocalTableGenerator, "generate_for_schemas", generate_fields)
    updates = []
    generation.run_generation(
        "test-job", scenario, {"id": "offline"}, output_dir=tmp_path,
        update_job=lambda job_id, update: updates.append(update),
    )
    final = updates[-1]
    assert final["status"] == expected_status
    assert bool(list(tmp_path.glob("*.csv"))) == (expected_status == "complete")
    if expected_status == "failed":
        assert final["download_url"] is None
        assert final["stats"]["violations"][0]["rule"] == "schema.code.enum"
        assert final["stats"]["evaluationResult"] == "fail"
