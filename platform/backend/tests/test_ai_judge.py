import pandas as pd
import pytest
from pydantic import ValidationError

from app.api.generation import _evaluation_report_html, _evaluation_report_payload
from app.evaluation import ai_judge
from app.evaluation.ai_judge import JudgeAuditOutcome
from app.job_queue import LostLease
from app.models.scenario import JudgeAuditResult, JobStats, ScenarioConfiguration
from app.services import generation as generation_service
from app.services.generation import _evaluation_metrics


def _scenario(record_count=30):
    return ScenarioConfiguration.model_validate({
        "title": "Healthcare workflow",
        "description": "Patient diagnosis and claim workflows",
        "recordCount": record_count,
        "secondaryMetric": {"label": "Tables", "value": "2"},
        "workflow": ["Patient", "Claim"],
        "paths": [
            {
                "name": "complete",
                "steps": ["Patient", "Claim"],
                "weight": 1,
                "overrides": {},
            },
            {
                "name": "patient_only",
                "steps": ["Patient"],
                "weight": 1,
                "overrides": {},
            },
        ],
        "rules": ["Diagnosis and procedure should be clinically coherent"],
        "schemas": {
            "Patient": {
                "patient_id": {"type": "integer", "constraints": {"primary_key": True}},
                "age": {"type": "integer", "constraints": {"min": 0, "max": 120}},
                "diagnosis": "text",
            },
            "Claim": {
                "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
                "patient_id": "foreign_key",
                "procedure": "text",
                "__foreign_keys__": {"patient_id": "Patient.patient_id"},
            },
        },
    })


def _tables(count=30, include_incomplete=False):
    patient_rows = []
    claim_rows = []
    for index in range(count):
        instance = f"workflow-{index}"
        patient_rows.append({
            "scenario_instance_id": instance,
            "scenario_path": "complete",
            "patient_id": index,
            "name": f"Sensitive Patient {index}",
            "email": f"patient{index}@example.test",
            "age": 40 + index % 20,
            "diagnosis": "Type 2 diabetes",
        })
        claim_rows.append({
            "scenario_instance_id": instance,
            "scenario_path": "complete",
            "claim_id": 1000 + index,
            "patient_id": index,
            "procedure": "Routine follow-up",
        })
    if include_incomplete:
        patient_rows.append({
            "scenario_instance_id": "patient-only",
            "scenario_path": "complete",
            "patient_id": 999,
            "age": 22,
            "diagnosis": "Asthma",
        })
        patient_rows.append({
            "scenario_instance_id": "valid-short-path",
            "scenario_path": "patient_only",
            "patient_id": 1000,
            "age": 30,
            "diagnosis": "Migraine",
        })
    return {
        "Patient": pd.DataFrame(patient_rows),
        "Claim": pd.DataFrame(claim_rows),
    }


def _stats(judge_audit=None):
    return JobStats.model_validate({
        "causalIntegrity": "100.0%",
        "referentialIntegrity": "100.0%",
        "compliance": "0.850" if judge_audit else "100.0%",
        "recordsGenerated": 60,
        "flaggedRecords": 0,
        "durationSeconds": 1.2,
        "tableRowCounts": {"Patient": 30, "Claim": 30},
        "pathCounts": {"complete": 30},
        "evaluationResult": "pass",
        "evaluationMetrics": [],
        "violations": [],
        **({"judgeAudit": judge_audit.model_dump(by_alias=True)} if judge_audit else {}),
    })


def test_judge_audit_result_validates_bounds_and_aliases():
    result = JudgeAuditResult.model_validate({
        "score": 0.85,
        "verdict": "warning",
        "reasoning": "Mostly plausible.",
        "flaggedAnomalies": ["One specialty mismatch."],
    })
    assert result.flagged_anomalies == ["One specialty mismatch."]
    assert result.model_dump(by_alias=True)["flaggedAnomalies"] == ["One specialty mismatch."]
    with pytest.raises(ValidationError):
        JudgeAuditResult(score=1.1, verdict="pass", reasoning="Invalid")


def test_job_stats_remains_compatible_without_judge_audit():
    stats = _stats()
    assert stats.judge_audit is None
    assert "judgeAudit" not in stats.model_dump(by_alias=True)


def test_workflow_sampling_is_path_aware_deterministic_and_minimized():
    tables = _tables(30, include_incomplete=True)
    scenario = _scenario(32)
    first = ai_judge.sample_complete_workflows("job-1", tables, scenario)
    second = ai_judge.sample_complete_workflows("job-1", tables, scenario)

    assert first == second
    assert len(first) == 30
    assert all(item["workflowId"].startswith("id_") for item in first)
    serialized = str(first)
    assert "Sensitive Patient" not in serialized
    assert "@example.test" not in serialized
    assert "scenario_instance_id" not in serialized
    assert "scenario_path" not in serialized

    complete = ai_judge.complete_workflows(tables, scenario)
    short_path = next(item for item in complete if item["path"] == "patient_only")
    assert set(short_path["tables"]) == {"Patient"}
    assert len(complete) == 31


@pytest.mark.parametrize("sample_size", [24, 51])
def test_sample_size_must_remain_within_card_scope(sample_size):
    with pytest.raises(ValueError, match="between 25 and 50"):
        ai_judge.sample_complete_workflows(
            "job-1", _tables(), _scenario(), sample_size=sample_size
        )


def test_offline_provider_skips_without_invoking_judge(monkeypatch):
    monkeypatch.setattr(
        ai_judge,
        "_request_structured_audit",
        lambda *_: pytest.fail("provider request should not run"),
    )
    outcome = ai_judge.run_ai_judge_audit(
        "job-1", _tables(), _scenario(), {"id": "offline"}
    )
    assert outcome.status == "not_configured"
    assert outcome.result is None


def test_provider_failure_is_contained_by_judge_boundary(monkeypatch):
    def fail(*_):
        raise TimeoutError("provider timed out with sensitive response text")

    monkeypatch.setattr(ai_judge, "_request_structured_audit", fail)
    outcome = ai_judge.run_ai_judge_audit(
        "job-1",
        _tables(),
        _scenario(),
        {"id": "openai", "model": "judge-model", "key": "secret"},
    )
    assert outcome.status == "unavailable"
    assert outcome.result is None
    assert "deterministic semantic checks were retained" in outcome.detail


def test_successful_audit_uses_minimized_workflows(monkeypatch):
    captured = {}

    def respond(provider, prompt):
        captured["provider"] = provider
        captured["prompt"] = prompt
        return JudgeAuditResult(
            score=0.82,
            verdict="warning",
            reasoning="Most workflows are plausible.",
            flaggedAnomalies=["One procedure needs review."],
        )

    monkeypatch.setattr(ai_judge, "_request_structured_audit", respond)
    outcome = ai_judge.run_ai_judge_audit(
        "job-1",
        _tables(),
        _scenario(),
        {"id": "openai", "model": "judge-model", "key": "secret"},
    )
    assert outcome.status == "completed"
    assert outcome.sampled_workflows == 30
    assert outcome.result and outcome.result.verdict == "warning"
    assert "Sensitive Patient" not in captured["prompt"]
    assert "@example.test" not in captured["prompt"]
    assert "secret" not in captured["prompt"]


@pytest.mark.parametrize(
    "verdict,expected_status,expected_result",
    [("pass", "pass", "partial"), ("warning", "pass", "partial"), ("fail", "fail", "fail")],
)
def test_judge_verdict_composes_with_semantic_correctness(
    verdict, expected_status, expected_result
):
    scenario = _scenario(3)
    scenario.paths = scenario.paths[:1]
    tables = _tables(3)
    tables["Patient"]["age"] = 40
    audit = JudgeAuditResult(
        score=0.4 if verdict == "fail" else 0.8,
        verdict=verdict,
        reasoning="Structured audit result.",
    )
    metrics, result, _, _ = _evaluation_metrics(
        tables,
        scenario.schemas,
        scenario,
        return_details=True,
        judge_audit=audit,
    )
    semantic = next(item for item in metrics if item.key == "semantic_correctness")
    assert semantic.status == expected_status
    assert semantic.value == f"{audit.score:.3f}"
    assert semantic.violations == 0
    assert result == expected_result


def test_deterministic_semantic_failure_wins_over_judge_pass():
    scenario = _scenario(1)
    tables = _tables(1)
    tables["Patient"].loc[0, "age"] = 150
    audit = JudgeAuditResult(score=0.95, verdict="pass", reasoning="Looks plausible.")
    metrics, result, _, _ = _evaluation_metrics(
        tables,
        scenario.schemas,
        scenario,
        return_details=True,
        judge_audit=audit,
    )
    semantic = next(item for item in metrics if item.key == "semantic_correctness")
    assert semantic.status == "fail"
    assert semantic.violations == 1
    assert result == "fail"


def test_report_payload_and_html_include_escaped_judge_audit():
    audit = JudgeAuditResult(
        score=0.85,
        verdict="warning",
        reasoning="Review <script>alert('reason')</script> carefully.",
        flaggedAnomalies=["Specialty <img src=x onerror=alert(1)> mismatch."],
    )
    stats = _stats(audit)
    payload = _evaluation_report_payload("job-1", stats)
    report = _evaluation_report_html("job-1", stats)

    assert payload["schemaVersion"] == "1.0"
    assert payload["judgeAudit"]["flaggedAnomalies"] == audit.flagged_anomalies
    assert "AI Judge Audit" in report
    assert "0.85 / 1.00" in report
    assert "&lt;script&gt;" in report
    assert "&lt;img" in report
    assert "<script>alert('reason')</script>" not in report


def test_historical_report_omits_json_audit_and_explains_html_state():
    stats = _stats()
    assert "judgeAudit" not in _evaluation_report_payload("historical", stats)
    assert "An AI Judge audit was not recorded for this run" in _evaluation_report_html(
        "historical", stats
    )


def test_judge_provider_failure_does_not_fail_or_retry_generation(monkeypatch, tmp_path):
    scenario = _scenario(3)
    scenario.schemas["Patient"].pop("age")
    monkeypatch.setattr(
        generation_service,
        "SyntheticDataGenerator",
        lambda **_: generation_service._LocalTableGenerator(),
    )

    def fail_request(*_):
        raise TimeoutError("judge provider unavailable")

    monkeypatch.setattr(ai_judge, "_request_structured_audit", fail_request)
    updates = []
    generation_service.run_generation(
        "job-provider-failure",
        scenario,
        {"id": "openai", "model": "judge-model", "key": "secret", "base_url": None},
        output_dir=tmp_path,
        update_job=lambda _, values: updates.append(values),
    )

    assert updates[-1]["status"] == "complete"
    assert "judgeAudit" not in updates[-1]["stats"]
    semantic = next(
        metric for metric in updates[-1]["stats"]["evaluationMetrics"]
        if metric["key"] == "semantic_correctness"
    )
    assert "AI Judge was unavailable" in semantic["detail"]
    assert list(tmp_path.glob("*.csv"))


def test_valid_judge_failure_uses_existing_quality_gate(monkeypatch, tmp_path):
    scenario = _scenario(3)
    scenario.schemas["Patient"].pop("age")
    audit = JudgeAuditResult(
        score=0.2,
        verdict="fail",
        reasoning="The sampled workflows are implausible.",
        flaggedAnomalies=["Diagnosis and procedure conflict."],
    )
    monkeypatch.setattr(
        generation_service,
        "run_ai_judge_audit",
        lambda *_args, **_kwargs: JudgeAuditOutcome(
            result=audit,
            status="completed",
            detail="AI Judge evaluated 3 complete workflow instances.",
            sampled_workflows=3,
        ),
    )
    updates = []
    generation_service.run_generation(
        "job-judge-fail",
        scenario,
        {"id": "offline"},
        output_dir=tmp_path,
        update_job=lambda _, values: updates.append(values),
    )

    assert updates[-1]["status"] == "failed"
    assert updates[-1]["stats"]["judgeAudit"]["verdict"] == "fail"
    assert not list(tmp_path.glob("*.csv"))


def test_lost_lease_before_judge_is_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        generation_service,
        "run_ai_judge_audit",
        lambda *_args, **_kwargs: pytest.fail("cancelled job must not call the Judge"),
    )

    def update_job(_, values):
        if values.get("progress") == 88:
            raise LostLease("cancelled")

    with pytest.raises(LostLease):
        generation_service.run_generation(
            "job-cancelled",
            _scenario(3),
            {"id": "offline"},
            output_dir=tmp_path,
            update_job=update_job,
        )
