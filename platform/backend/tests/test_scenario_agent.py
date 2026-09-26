from app.agents.scenario_agent import (
    DOMAIN_TEMPLATES,
    _heuristic_compile_scenario,
    _validate_scenario_candidate,
)
from app.models.scenario import ScenarioConfiguration


def test_heuristic_fallback_does_not_invent_a_scenario_for_a_greeting():
    response = _heuristic_compile_scenario("hello")
    assert response.scenario is None
    assert "What kind of data" in response.message


def test_heuristic_fallback_does_not_claim_a_draft_was_created():
    response = _heuristic_compile_scenario(
        "Generate 20 claims for healthcare insurance"
    )
    assert response.scenario is None
    assert "couldn’t create a scenario" in response.message


def test_insurance_template_has_valid_machine_checks():
    candidate = ScenarioConfiguration.model_validate(
        {**DOMAIN_TEMPLATES[0], "record_count": 20}
    )
    validated, errors = _validate_scenario_candidate(candidate)

    assert errors == []
    assert validated is not None
    assert validated.record_count == 20
    assert validated.secondary_metric.label == "Denial rate"
    assert {check.kind for check in validated.checks} == {
        "allowed_values",
        "temporal_order",
        "absence_by_value",
    }


def test_agent_rejects_machine_check_with_unknown_field():
    candidate = ScenarioConfiguration.model_validate(DOMAIN_TEMPLATES[0])
    candidate.checks[0].field = "missing_status"

    validated, errors = _validate_scenario_candidate(candidate)

    assert validated is None
    assert any("unknown field 'Claim.missing_status'" in error for error in errors)
