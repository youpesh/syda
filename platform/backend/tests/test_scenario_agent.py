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
        "state_machine",
        "semantic_invariant",
    }


def test_agent_rejects_machine_check_with_unknown_field():
    candidate = ScenarioConfiguration.model_validate(DOMAIN_TEMPLATES[0])
    candidate.checks[0].field = "missing_status"

    validated, errors = _validate_scenario_candidate(candidate)

    assert validated is None
    assert any("unknown field 'Claim.missing_status'" in error for error in errors)


def test_agent_rejects_path_missing_required_parent():
    candidate = ScenarioConfiguration.model_validate({
        **DOMAIN_TEMPLATES[1],
        "paths": [{"name": "Holiday Season Order", "steps": ["Order", "Payment"]}],
    })

    validated, errors = _validate_scenario_candidate(candidate)

    assert validated is None
    assert errors == [
        "Scenario path 'Holiday Season Order' includes 'Order' without required parent step 'Customer'."
    ]


def test_agent_accepts_path_with_required_parents():
    candidate = ScenarioConfiguration.model_validate({
        **DOMAIN_TEMPLATES[1],
        "paths": [{"name": "Holiday Season Order", "steps": ["Customer", "Product", "Order", "Payment"]}],
    })

    validated, errors = _validate_scenario_candidate(candidate)

    assert validated is not None
    assert errors == []


def test_loan_origination_template_matches_and_validates():
    from app.agents.scenario_agent import _starter_scenario_for_context

    loan_starter = _starter_scenario_for_context("Create a loan approval scenario with 50 instances")
    assert loan_starter.title == "Loan origination & underwriting"
    assert loan_starter.secondary_metric.label == "Rejection rate"
    assert "Underwriting" in loan_starter.workflow
    assert "Disbursement" in loan_starter.workflow

    validated, errors = _validate_scenario_candidate(loan_starter)
    assert errors == []
    assert validated is not None


def test_auto_reconcile_translates_plain_english_absence_rule():
    from app.agents.scenario_agent import _auto_reconcile_scenario_candidate

    raw_candidate = {
        "title": "Natural Language Test",
        "description": "Test non-technical business input",
        "record_count": 50,
        "workflow": ["Applicant", "Underwriting", "Disbursement"],
        "rules": [
            "Rejected loans cannot be disbursed",
        ],
        "checks": [],
    }

    reconciled = _auto_reconcile_scenario_candidate(raw_candidate)
    assert "Disbursement" in reconciled["schemas"]
    assert "status" in reconciled["schemas"]["Underwriting"]

    absence_checks = [c for c in reconciled["checks"] if c["kind"] == "absence_by_value"]
    assert len(absence_checks) == 1
    assert absence_checks[0]["source"] == "Underwriting.status"
    assert absence_checks[0]["value"] == "rejected"
    assert absence_checks[0]["absent_table"] == "Disbursement"


def test_auto_reconcile_normalizes_dotless_absence_source():
    from app.agents.scenario_agent import _auto_reconcile_scenario_candidate

    raw_candidate = {
        "title": "Dotless Source Test",
        "description": "Test source without dot",
        "record_count": 50,
        "workflow": ["Order", "Shipment"],
        "checks": [
            {
                "name": "no_shipment_on_cancelled",
                "kind": "absence_by_value",
                "source": "Order",
                "value": "cancelled",
                "absent_table": "Shipment",
            }
        ],
    }

    reconciled = _auto_reconcile_scenario_candidate(raw_candidate)
    assert reconciled["checks"][0]["source"] == "Order.status"
    assert "status" in reconciled["schemas"]["Order"]
