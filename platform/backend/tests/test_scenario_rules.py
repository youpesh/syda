from datetime import date

import pandas as pd

from app.agents.scenario_agent import DOMAIN_TEMPLATES
from app.api.generation import (
    _LocalTableGenerator,
    _evaluation_metrics,
    _scenario_definition,
)
from app.models.scenario import ScenarioConfiguration
from syda.scenarios import ScenarioEngine


def _insurance_scenario():
    payload = {**DOMAIN_TEMPLATES[0], "record_count": 20}
    return ScenarioConfiguration.model_validate(payload)


def test_insurance_rule_text_and_checks_reach_engine_and_pass():
    scenario = _insurance_scenario()
    definition = _scenario_definition(scenario, scenario.schemas or {})
    assert definition.rules == scenario.rules
    assert definition.secondary_metric == {"label": "Denial rate", "value": "15%"}
    assert len(definition.checks) == len(scenario.checks) == 6

    generated = ScenarioEngine(_LocalTableGenerator()).generate(
        definition, scenario.record_count, start_at=date(2026, 1, 1)
    )
    checks = ScenarioEngine.evaluate_checks(definition, generated.tables)
    assert all(item["violations"] == 0 for item in checks)

    metrics, result, violations = _evaluation_metrics(
        generated.tables, scenario.schemas or {}, scenario
    )
    rules_metric = next(item for item in metrics if item.key == "declared_rules")
    assert rules_metric.status == "pass"
    assert result in {"pass", "partial"}
    assert violations == 0


def test_evaluation_fails_when_denied_claim_has_payment():
    scenario = _insurance_scenario()
    definition = _scenario_definition(scenario, scenario.schemas or {})
    generated = ScenarioEngine(_LocalTableGenerator()).generate(
        definition, scenario.record_count, start_at=date(2026, 1, 1)
    )
    denied = generated.tables["Claim"].query("status == 'denied'").iloc[0]
    payment = generated.tables["Payment"].iloc[[0]].copy()
    payment["claim_id"] = denied["claim_id"]
    payment["scenario_instance_id"] = denied["scenario_instance_id"]
    generated.tables["Payment"] = pd.concat(
        [generated.tables["Payment"], payment], ignore_index=True
    )

    checks = ScenarioEngine.evaluate_checks(definition, generated.tables)
    denied_check = next(item for item in checks if item["name"] == "denied_claims_have_no_payment")
    assert denied_check["violations"] == 1

    metrics, result, violations = _evaluation_metrics(
        generated.tables, scenario.schemas or {}, scenario
    )
    rules_metric = next(item for item in metrics if item.key == "declared_rules")
    assert rules_metric.status == "fail"
    assert result == "fail"
    assert violations >= 1
