import pandas as pd
import pytest

from app.api.generation import _evaluation_metrics, _LocalTableGenerator
from app.models.scenario import ScenarioConfiguration, JobStats
from syda.scenarios import ScenarioEngine, ScenarioCheck


def _insurance_causal_scenario():
    return ScenarioConfiguration.model_validate({
        "title": "Causal Insurance Workflow",
        "description": "Insurance claims with causal state machine and temporal validation",
        "recordCount": 10,
        "secondaryMetric": {"label": "Denial rate", "value": "20%"},
        "workflow": ["Diagnosis", "Claim"],
        "paths": [
            {
                "name": "approved",
                "steps": ["Diagnosis", "Claim"],
                "weight": 0.8,
                "overrides": {"Claim": {"status": "paid"}},
            },
            {
                "name": "denied",
                "steps": ["Diagnosis", "Claim"],
                "weight": 0.2,
                "overrides": {"Claim": {"status": "denied"}},
            },
        ],
        "rules": [
            "Diagnosis occurs before claim creation",
            "Paid claims must have positive payment amounts",
            "Denied claims must have zero payment amount",
        ],
        "checks": [
            {
                "name": "temporal_diagnosis_before_claim",
                "kind": "temporal_order",
                "chain": ["Diagnosis.diagnosis_date", "Claim.claim_date"],
                "strict": False,
            },
            {
                "name": "paid_claim_has_positive_payment",
                "kind": "semantic_invariant",
                "table": "Claim",
                "condition": "status == 'paid'",
                "assertion": "payment_amount > 0",
            },
            {
                "name": "denied_claim_has_zero_payment",
                "kind": "semantic_invariant",
                "table": "Claim",
                "condition": "status == 'denied'",
                "assertion": "payment_amount == 0",
            },
            {
                "name": "claim_status_fsm",
                "kind": "state_transition",
                "table": "Claim",
                "field": "status",
                "transitions": {
                    "submitted": ["under review"],
                    "under review": ["approved", "denied"],
                    "approved": ["paid"],
                    "paid": [],
                    "denied": [],
                },
            },
        ],
        "schemas": {
            "Diagnosis": {
                "diagnosis_id": {"type": "integer", "constraints": {"primary_key": True}},
                "diagnosis_date": "date",
            },
            "Claim": {
                "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
                "diagnosis_id": "foreign_key",
                "diagnosis_date": "date",
                "claim_date": "date",
                "status": "text",
                "payment_amount": "float",
                "__foreign_keys__": {"diagnosis_id": "Diagnosis.diagnosis_id"},
            },
        },
    })


def test_evaluation_metrics_returns_structured_violations():
    scenario = _insurance_causal_scenario()
    dfs = {
        "Diagnosis": pd.DataFrame([
            {"scenario_instance_id": "c1", "diagnosis_id": 1, "diagnosis_date": "2026-01-01"},
            {"scenario_instance_id": "c2", "diagnosis_id": 2, "diagnosis_date": "2026-02-01"},
        ]),
        "Claim": pd.DataFrame([
            # Valid paid claim
            {
                "scenario_instance_id": "c1",
                "claim_id": 101,
                "diagnosis_id": 1,
                "claim_date": "2026-01-10",
                "status": "paid",
                "payment_amount": 250.0,
            },
            # Invalid: denied claim with non-zero payment AND invalid status jump
            {
                "scenario_instance_id": "c2",
                "claim_id": 102,
                "diagnosis_id": 2,
                "claim_date": "2026-02-10",
                "status": "invalid_status",
                "payment_amount": 100.0,
            },
        ]),
    }

    metrics, result, total_violations, violation_records = _evaluation_metrics(
        dfs, scenario.schemas or {}, scenario, return_details=True
    )

    assert total_violations >= 1
    assert result == "fail"
    assert len(violation_records) >= 1
    # Check that structured violation contains table, row, rule, error
    first_v = violation_records[0]
    assert hasattr(first_v, "table")
    assert hasattr(first_v, "row")
    assert hasattr(first_v, "rule")
    assert hasattr(first_v, "error")
    assert first_v.table == "Claim"


def test_job_stats_serializes_violations():
    stats = JobStats(
        causalIntegrity="100.0%",
        referentialIntegrity="100.0%",
        compliance="95.0%",
        recordsGenerated=100,
        flaggedRecords=1,
        durationSeconds=1.2,
        tableRowCounts={"Claim": 100},
        pathCounts={"approved": 100},
        evaluationMetrics=[],
        evaluationResult="fail",
        violations=[
            {
                "table": "Claim",
                "row": "claim-001",
                "rule": "paid_claim_has_positive_payment",
                "error": "Semantic invariant failed: payment_amount > 0",
            }
        ],
    )
    dumped = stats.model_dump(by_alias=True)
    assert "violations" in dumped
    assert len(dumped["violations"]) == 1
    assert dumped["violations"][0]["table"] == "Claim"
    assert dumped["violations"][0]["rule"] == "paid_claim_has_positive_payment"
