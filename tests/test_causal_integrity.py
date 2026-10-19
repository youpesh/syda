"""Tests for Feature 3: Causal State Machine & Temporal Order Engine.

Enforces:
1. Temporal ordering across workflow steps (e.g., diagnosis_date <= claim_date <= adjudication_date <= payment_date).
2. Sequential state machine transitions (e.g., submitted -> under review -> approved/denied -> paid) with no illegal jumps.
3. Semantic invariants (e.g., paid claims must have payment_amount > 0; denied claims must have 0).
4. Post-generation validation pass returning structured violation reports (table, row, rule, error).
5. Custom rule definitions enabling domain-specific constraints without modifying library code.
"""

from datetime import date
import pandas as pd
import pytest

from syda.scenarios import (
    ScenarioCheck,
    ScenarioDefinition,
    ScenarioEngine,
    ScenarioStep,
    ViolationReport,
)


def _base_schemas():
    return {
        "Diagnosis": {
            "diagnosis_id": {"type": "integer", "constraints": {"primary_key": True}},
            "patient_id": "text",
            "diagnosis_date": "date",
        },
        "Claim": {
            "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
            "patient_id": "text",
            "diagnosis_date": "date",
            "claim_date": "date",
            "adjudication_date": "date",
            "payment_date": "date",
            "status": "text",
            "previous_status": "text",
            "claim_amount": "float",
            "payment_amount": "float",
        },
        "ClaimStatusHistory": {
            "history_id": {"type": "integer", "constraints": {"primary_key": True}},
            "claim_id": "integer",
            "status": "text",
            "timestamp": "date",
        },
    }


def test_temporal_chain_single_table_enforcement():
    """Verify diagnosis_date <= claim_date <= adjudication_date <= payment_date on single table."""
    schemas = _base_schemas()
    definition = ScenarioDefinition(
        name="TemporalChainTest",
        schemas=schemas,
        steps=[
            ScenarioStep(name="Claim", table="Claim"),
        ],
        checks=[
            ScenarioCheck(
                name="claim_workflow_chronological_order",
                kind="temporal_order",
                table="Claim",
                chain=[
                    "diagnosis_date",
                    "claim_date",
                    "adjudication_date",
                    "payment_date",
                ],
                strict=False,
            )
        ],
    )

    # Valid chronological order: diagnosis <= claim <= adjudication <= payment
    valid_claim_df = pd.DataFrame([
        {
            "scenario_instance_id": "inst-001",
            "claim_id": 1,
            "patient_id": "p1",
            "diagnosis_date": "2026-01-01",
            "claim_date": "2026-01-05",
            "adjudication_date": "2026-01-10",
            "payment_date": "2026-01-15",
            "status": "paid",
            "previous_status": "approved",
            "claim_amount": 100.0,
            "payment_amount": 80.0,
        },
        {
            "scenario_instance_id": "inst-002",
            "claim_id": 2,
            "patient_id": "p2",
            "diagnosis_date": "2026-02-01",
            "claim_date": "2026-02-01",  # equal timestamps are allowed when strict=False
            "adjudication_date": "2026-02-05",
            "payment_date": "2026-02-10",
            "status": "paid",
            "previous_status": "approved",
            "claim_amount": 200.0,
            "payment_amount": 150.0,
        },
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(
        definition, {"Claim": valid_claim_df}
    )
    assert len(violations) == 0
    assert results[0]["violations"] == 0

    # Invalid chronological order: adjudication_date (Jan 10) earlier than claim_date (Jan 12)
    invalid_claim_df = valid_claim_df.copy()
    invalid_claim_df.loc[0, "claim_date"] = "2026-01-12"

    results, violations = ScenarioEngine.validate_causal_workflow(
        definition, {"Claim": invalid_claim_df}
    )
    assert len(violations) == 1
    assert violations[0].table == "Claim"
    assert violations[0].rule == "claim_workflow_chronological_order"
    assert "claim_date" in violations[0].error
    assert "adjudication_date" in violations[0].error


def test_temporal_chain_multi_table_enforcement():
    """Verify chronological ordering across multiple related tables by instance ID."""
    schemas = _base_schemas()
    definition = ScenarioDefinition(
        name="MultiTableTemporalChainTest",
        schemas=schemas,
        steps=[
            ScenarioStep(name="Diagnosis", table="Diagnosis"),
            ScenarioStep(name="Claim", table="Claim"),
        ],
        checks=[
            ScenarioCheck(
                name="cross_table_temporal_order",
                kind="temporal_order",
                chain=[
                    "Diagnosis.diagnosis_date",
                    "Claim.claim_date",
                    "Claim.payment_date",
                ],
                strict=True,
            )
        ],
    )

    diagnosis_df = pd.DataFrame([
        {"scenario_instance_id": "inst-1", "diagnosis_id": 1, "diagnosis_date": "2026-01-01"},
        {"scenario_instance_id": "inst-2", "diagnosis_id": 2, "diagnosis_date": "2026-03-01"},
    ])
    # inst-1 is valid; inst-2 has claim_date earlier than diagnosis_date
    claim_df = pd.DataFrame([
        {"scenario_instance_id": "inst-1", "claim_id": 101, "claim_date": "2026-01-05", "payment_date": "2026-01-20"},
        {"scenario_instance_id": "inst-2", "claim_id": 102, "claim_date": "2026-02-15", "payment_date": "2026-03-10"},
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(
        definition, {"Diagnosis": diagnosis_df, "Claim": claim_df}
    )
    assert len(violations) == 1
    assert violations[0].table == "Claim"
    assert violations[0].row == "inst-2"
    assert "Diagnosis.diagnosis_date" in violations[0].error


def test_finite_state_transition_validator_event_sequence():
    """Verify sequential state machine transitions (submitted -> under review -> approved/denied -> paid)."""
    schemas = _base_schemas()
    transitions = {
        "submitted": ["under review"],
        "under review": ["approved", "denied"],
        "approved": ["paid"],
        "denied": [],
        "paid": [],
    }

    definition = ScenarioDefinition(
        name="FSMSequenceTest",
        schemas=schemas,
        steps=[ScenarioStep(name="ClaimStatusHistory", table="ClaimStatusHistory")],
        checks=[
            ScenarioCheck(
                name="claim_status_fsm",
                kind="state_transition",
                table="ClaimStatusHistory",
                field="status",
                order_by="timestamp",
                initial_states=["submitted"],
                transitions=transitions,
            )
        ],
    )

    # Valid lifecycle 1: submitted -> under review -> approved -> paid
    # Valid lifecycle 2: submitted -> under review -> denied
    valid_events = pd.DataFrame([
        {"scenario_instance_id": "claim-1", "status": "submitted", "timestamp": "2026-01-01"},
        {"scenario_instance_id": "claim-1", "status": "under review", "timestamp": "2026-01-02"},
        {"scenario_instance_id": "claim-1", "status": "approved", "timestamp": "2026-01-03"},
        {"scenario_instance_id": "claim-1", "status": "paid", "timestamp": "2026-01-04"},
        {"scenario_instance_id": "claim-2", "status": "submitted", "timestamp": "2026-01-01"},
        {"scenario_instance_id": "claim-2", "status": "under review", "timestamp": "2026-01-02"},
        {"scenario_instance_id": "claim-2", "status": "denied", "timestamp": "2026-01-03"},
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(
        definition, {"ClaimStatusHistory": valid_events}
    )
    assert len(violations) == 0

    # Illegal jump 1: submitted directly to paid (skipping under review and approved)
    # Illegal jump 2: denied transitioning to paid
    invalid_events = pd.DataFrame([
        # Illegal jump: submitted -> paid
        {"scenario_instance_id": "claim-illegal-1", "status": "submitted", "timestamp": "2026-01-01"},
        {"scenario_instance_id": "claim-illegal-1", "status": "paid", "timestamp": "2026-01-02"},
        # Illegal jump: denied -> paid
        {"scenario_instance_id": "claim-illegal-2", "status": "submitted", "timestamp": "2026-01-01"},
        {"scenario_instance_id": "claim-illegal-2", "status": "under review", "timestamp": "2026-01-02"},
        {"scenario_instance_id": "claim-illegal-2", "status": "denied", "timestamp": "2026-01-03"},
        {"scenario_instance_id": "claim-illegal-2", "status": "paid", "timestamp": "2026-01-04"},
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(
        definition, {"ClaimStatusHistory": invalid_events}
    )
    assert len(violations) == 2
    assert any("submitted" in v.error and "paid" in v.error for v in violations)
    assert any("denied" in v.error and "paid" in v.error for v in violations)


def test_finite_state_transition_validator_row_transition():
    """Verify finite state transition validation on row-level from/to status fields."""
    schemas = _base_schemas()
    transitions = {
        "submitted": ["under review"],
        "under review": ["approved", "denied"],
        "approved": ["paid"],
        "denied": [],
        "paid": [],
    }

    definition = ScenarioDefinition(
        name="FSMRowTransitionTest",
        schemas=schemas,
        steps=[ScenarioStep(name="Claim", table="Claim")],
        checks=[
            ScenarioCheck(
                name="claim_row_status_fsm",
                kind="state_machine",
                table="Claim",
                field="status",
                previous_field="previous_status",
                transitions=transitions,
            )
        ],
    )

    df = pd.DataFrame([
        {"scenario_instance_id": "c1", "status": "under review", "previous_status": "submitted"},
        {"scenario_instance_id": "c2", "status": "paid", "previous_status": "approved"},
        {"scenario_instance_id": "c3", "status": "paid", "previous_status": "denied"},  # ILLEGAL
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Claim": df})
    assert len(violations) == 1
    assert violations[0].table == "Claim"
    assert violations[0].row == "c3"
    assert "denied" in violations[0].error and "paid" in violations[0].error


def test_semantic_invariants_paid_and_denied_amounts():
    """Verify semantic invariants: paid claims must have payment_amount > 0; denied claims must have 0."""
    schemas = _base_schemas()
    definition = ScenarioDefinition(
        name="SemanticInvariantsTest",
        schemas=schemas,
        steps=[ScenarioStep(name="Claim", table="Claim")],
        checks=[
            ScenarioCheck(
                name="paid_claim_has_positive_payment",
                kind="semantic_invariant",
                table="Claim",
                condition="status == 'paid'",
                assertion="payment_amount > 0",
            ),
            ScenarioCheck(
                name="denied_claim_has_zero_payment",
                kind="semantic_invariant",
                table="Claim",
                condition="status == 'denied'",
                assertion="payment_amount == 0",
            ),
        ],
    )

    # Valid dataset
    valid_df = pd.DataFrame([
        {"scenario_instance_id": "c1", "status": "paid", "payment_amount": 150.0},
        {"scenario_instance_id": "c2", "status": "denied", "payment_amount": 0.0},
        {"scenario_instance_id": "c3", "status": "under review", "payment_amount": 0.0},
    ])
    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Claim": valid_df})
    assert len(violations) == 0

    # Invalid dataset: paid with 0 amount, denied with 250 amount
    invalid_df = pd.DataFrame([
        {"scenario_instance_id": "bad-paid", "status": "paid", "payment_amount": 0.0},
        {"scenario_instance_id": "bad-denied", "status": "denied", "payment_amount": 250.0},
    ])
    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Claim": invalid_df})
    assert len(violations) == 2
    assert any(v.row == "bad-paid" and "paid_claim_has_positive_payment" in v.rule for v in violations)
    assert any(v.row == "bad-denied" and "denied_claim_has_zero_payment" in v.rule for v in violations)


def test_custom_rule_definition_without_modifying_library():
    """Verify custom domain-specific rule definitions evaluate smoothly."""
    schemas = _base_schemas()
    definition = ScenarioDefinition(
        name="CustomRuleTest",
        schemas=schemas,
        steps=[ScenarioStep(name="Claim", table="Claim")],
        checks=[
            ScenarioCheck(
                name="payment_cannot_exceed_claim_amount",
                kind="custom_rule",
                table="Claim",
                expression="payment_amount <= claim_amount",
            )
        ],
    )

    df = pd.DataFrame([
        {"scenario_instance_id": "c1", "claim_amount": 500.0, "payment_amount": 400.0},
        {"scenario_instance_id": "c2", "claim_amount": 100.0, "payment_amount": 150.0},  # Exceeds!
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Claim": df})
    assert len(violations) == 1
    assert violations[0].row == "c2"
    assert violations[0].rule == "payment_cannot_exceed_claim_amount"


def test_automatic_causal_inference_without_explicit_checks():
    """Verify that a scenario with NO manually specified checks automatically infers and enforces causal integrity."""
    schemas = _base_schemas()
    # Notice: checks is empty! Non-technical user only defined steps and schemas
    definition = ScenarioDefinition(
        name="AutoInferenceTest",
        schemas=schemas,
        steps=[
            ScenarioStep(name="Diagnosis", table="Diagnosis", timestamp_field="diagnosis_date"),
            ScenarioStep(name="Claim", table="Claim", timestamp_field="claim_date"),
        ],
        checks=[],
    )

    # Valid data: diagnosis precedes claim, payment_date follows claim_date, paid claim has positive amount
    valid_claim_df = pd.DataFrame([
        {
            "scenario_instance_id": "inst-1",
            "claim_id": 1,
            "diagnosis_date": "2026-01-01",
            "claim_date": "2026-01-05",
            "adjudication_date": "2026-01-10",
            "payment_date": "2026-01-15",
            "status": "paid",
            "payment_amount": 100.0,
        }
    ])
    valid_diag_df = pd.DataFrame([
        {
            "scenario_instance_id": "inst-1",
            "diagnosis_id": 1,
            "diagnosis_date": "2026-01-01",
        }
    ])
    results, violations = ScenarioEngine.validate_causal_workflow(
        definition,
        {"Diagnosis": valid_diag_df, "Claim": valid_claim_df},
    )
    assert len(violations) == 0

    # Invalid data:
    # 1. Temporal inversion: payment_date is earlier than adjudication_date
    invalid_claim_df = pd.DataFrame([
        {
            "scenario_instance_id": "inst-bad",
            "claim_id": 2,
            "diagnosis_date": "2026-01-01",
            "claim_date": "2026-01-05",
            "adjudication_date": "2026-01-20",
            "payment_date": "2026-01-10",  # Earlier than adjudication!
            "status": "paid",
            "payment_amount": 0.0,
        }
    ])
    results, violations = ScenarioEngine.validate_causal_workflow(
        definition,
        {"Diagnosis": valid_diag_df, "Claim": invalid_claim_df},
    )
    assert len(violations) >= 1
    assert any("lifecycle" in v.rule or "temporal" in v.rule for v in violations)


def test_resolved_paths_automatically_infers_branching_from_absence_check():
    """Verify that absence_by_value checks automatically infer branching paths (approved vs rejected) when paths is empty."""
    schemas = {
        "Applicant": {"applicant_id": {"type": "integer", "constraints": {"primary_key": True}}},
        "Application": {"application_id": {"type": "integer", "constraints": {"primary_key": True}}},
        "Underwriting": {"underwriting_id": {"type": "integer", "constraints": {"primary_key": True}}, "status": "text"},
        "Disbursement": {
            "disbursement_id": {"type": "integer", "constraints": {"primary_key": True}},
            "underwriting_id": "integer",
            "__foreign_keys__": {"underwriting_id": "Underwriting.underwriting_id"},
        },
        "Repayment": {
            "repayment_id": {"type": "integer", "constraints": {"primary_key": True}},
            "disbursement_id": "integer",
            "__foreign_keys__": {"disbursement_id": "Disbursement.disbursement_id"},
        },
    }

    definition = ScenarioDefinition(
        name="AutoBranchingTest",
        schemas=schemas,
        steps=[
            ScenarioStep(name="Applicant", table="Applicant"),
            ScenarioStep(name="Application", table="Application"),
            ScenarioStep(name="Underwriting", table="Underwriting"),
            ScenarioStep(name="Disbursement", table="Disbursement"),
            ScenarioStep(name="Repayment", table="Repayment"),
        ],
        secondaryMetric={"label": "Rejection rate", "value": "20%"},
        checks=[
            ScenarioCheck(
                name="no_disbursement_for_rejected",
                kind="absence_by_value",
                source="Underwriting.status",
                value="rejected",
                absent_table="Disbursement",
            )
        ],
    )

    paths = definition.resolved_paths()
    assert len(paths) == 2
    approved_path = next(p for p in paths if p.name == "approved")
    rejected_path = next(p for p in paths if p.name == "rejected")

    assert approved_path.weight == 0.8
    assert approved_path.steps == ["Applicant", "Application", "Underwriting", "Disbursement", "Repayment"]

    assert rejected_path.weight == 0.2
    # Both Disbursement and its child Repayment must be absent!
    assert rejected_path.steps == ["Applicant", "Application", "Underwriting"]
    assert rejected_path.overrides == {"Underwriting": {"status": "rejected"}}


def test_customer_state_allowed_values_passes_without_fsm_interference():
    """Verify that domain-agnostic inference does not flag geographic fields like Customer.state = 'CA' as invalid FSM states."""
    schemas = {
        "Customer": {
            "customer_id": {"type": "integer", "constraints": {"primary_key": True}},
            "state": "text",
            "created_at": "date",
        }
    }
    definition = ScenarioDefinition(
        name="CustomerStateTest",
        schemas=schemas,
        steps=[ScenarioStep(name="Customer", table="Customer", timestamp_field="created_at")],
        checks=[
            ScenarioCheck(
                name="valid_us_states",
                kind="allowed_values",
                table="Customer",
                field="state",
                values=["CA", "NY", "TX", "WA", "FL"],
            )
        ],
    )
    df = pd.DataFrame([
        {"scenario_instance_id": "cust-1", "customer_id": 1, "state": "CA", "created_at": "2026-01-01"},
        {"scenario_instance_id": "cust-2", "customer_id": 2, "state": "NY", "created_at": "2026-01-02"},
    ])
    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Customer": df})
    assert len(violations) == 0


def test_restricted_expression_evaluator_prevents_code_execution(tmp_path):
    """Verify that AST-restricted expression evaluation blocks sandbox escape and arbitrary code execution."""
    pwn_target = tmp_path / "pwned.txt"
    malicious_expr = (
        f"[c for c in ().__class__.__bases__[0].__subclasses__() if c.__name__ == 'BuiltinImporter']"
        f"[0]().load_module('builtins').open(r'{pwn_target}', 'w').write('exploited')"
    )

    schemas = _base_schemas()
    definition = ScenarioDefinition(
        name="SecurityExploitTest",
        schemas=schemas,
        steps=[ScenarioStep(name="Claim", table="Claim")],
        checks=[
            ScenarioCheck(
                name="exploit_check",
                kind="custom_rule",
                table="Claim",
                expression=malicious_expr,
            )
        ],
    )
    df = pd.DataFrame([{"scenario_instance_id": "c1", "status": "paid", "payment_amount": 100.0}])
    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Claim": df})

    # The file must NOT be created on disk
    assert not pwn_target.exists()
    # A violation must be reported due to restricted expression failure
    assert len(violations) == 1
    assert "Failed to evaluate" in violations[0].error or "disallowed" in violations[0].error.lower() or "forbidden" in violations[0].error.lower()


def test_temporal_chain_catches_out_of_order_earlier_max_vs_later_min():
    """Verify that multi-row temporal chains compare earlier step's maximum against later step's minimum."""
    schemas = {
        "Order": {
            "order_id": {"type": "integer", "constraints": {"primary_key": True}},
            "order_date": "date",
        },
        "Shipment": {
            "shipment_id": {"type": "integer", "constraints": {"primary_key": True}},
            "ship_date": "date",
        },
    }
    definition = ScenarioDefinition(
        name="MultiRowTemporalChainTest",
        schemas=schemas,
        steps=[
            ScenarioStep(name="Order", table="Order", timestamp_field="order_date"),
            ScenarioStep(name="Shipment", table="Shipment", timestamp_field="ship_date"),
        ],
        checks=[
            ScenarioCheck(
                name="order_before_shipment_chain",
                kind="temporal_order",
                chain=["Order.order_date", "Shipment.ship_date"],
            )
        ],
    )

    # Instance 1: Order has rows on Jan 1 and Jan 10. Shipment has a row on Jan 5.
    # Earlier max (Jan 10) > Later min (Jan 5), so this MUST trigger a violation!
    order_df = pd.DataFrame([
        {"scenario_instance_id": "inst-1", "order_id": 1, "order_date": "2026-01-01"},
        {"scenario_instance_id": "inst-1", "order_id": 2, "order_date": "2026-01-10"},
    ])
    shipment_df = pd.DataFrame([
        {"scenario_instance_id": "inst-1", "shipment_id": 101, "ship_date": "2026-01-05"},
    ])

    results, violations = ScenarioEngine.validate_causal_workflow(
        definition,
        {"Order": order_df, "Shipment": shipment_df},
    )
    assert any(v.rule == "order_before_shipment_chain" and v.table == "Shipment" for v in violations)
    chain_v = next(v for v in violations if v.rule == "order_before_shipment_chain")
    assert "must be <=" in chain_v.error and "2026-01-10" in chain_v.error and "2026-01-05" in chain_v.error


def test_invalid_semantic_condition_fails_validation_and_resolves_table_prefix():
    """Verify that table-prefixed fields resolve properly, and invalid conditions record violations instead of being silently skipped."""
    schemas = _base_schemas()
    definition = ScenarioDefinition(
        name="PrefixAndSyntaxTest",
        schemas=schemas,
        steps=[ScenarioStep(name="Claim", table="Claim")],
        checks=[
            # 1. Condition with table prefix (Claim.status)
            ScenarioCheck(
                name="prefixed_status_check",
                kind="semantic_invariant",
                table="Claim",
                condition="Claim.status == 'paid'",
                assertion="payment_amount > 0",
            ),
            # 2. Condition referencing non-existent column (should flag violation, NOT silently skip)
            ScenarioCheck(
                name="missing_field_condition",
                kind="semantic_invariant",
                table="Claim",
                condition="non_existent_column == 'foo'",
                assertion="payment_amount > 0",
            ),
        ],
    )

    df = pd.DataFrame([
        {"scenario_instance_id": "c1", "status": "paid", "payment_amount": 50.0},
    ])
    results, violations = ScenarioEngine.validate_causal_workflow(definition, {"Claim": df})

    # The prefixed condition succeeded (paid with 50.0 > 0).
    # The non_existent_column condition failed to evaluate and flagged a violation!
    assert len(violations) == 1
    assert violations[0].rule == "missing_field_condition"
    assert "Failed to evaluate semantic condition" in violations[0].error



