from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from syda.scenarios import (
    ScenarioBinding,
    ScenarioDefinition,
    ScenarioEngine,
    ScenarioPath,
    ScenarioStep,
)


class FakeTableGenerator:
    """Small deterministic stand-in for Syda's existing table generator."""

    def __init__(self):
        self.sample_sizes = {}
        self.schemas = {}
        self.prompts = {}
        self.calls = []

    def generate_for_schemas(self, schemas, prompts=None, sample_sizes=None, **kwargs):
        self.schemas = schemas
        self.sample_sizes = dict(sample_sizes or {})
        self.prompts = dict(prompts or {})
        self.calls.append(
            {
                "schemas": schemas,
                "prompts": prompts,
                "sample_sizes": dict(sample_sizes or {}),
            }
        )
        generated = {}
        for table_index, (table, schema) in enumerate(schemas.items(), start=1):
            rows = []
            for row_index in range(self.sample_sizes[table]):
                row = {}
                for field, definition in schema.items():
                    if field.startswith("__"):
                        continue
                    field_type = (
                        definition
                        if isinstance(definition, str)
                        else definition.get("type", "text")
                    )
                    constraints = (
                        definition.get("constraints", {})
                        if isinstance(definition, dict)
                        else {}
                    )
                    if field == "syda_scenario_row_key":
                        row[field] = constraints["enum"][row_index]
                    elif constraints.get("primary_key"):
                        row[field] = table_index * 10_000 + row_index + 1
                    elif field_type == "foreign_key":
                        row[field] = -1
                    elif field_type in ("date", "datetime"):
                        row[field] = "1999-01-01"
                    elif field == "state":
                        row[field] = ("CA", "NY", "TX")[row_index % 3]
                    else:
                        row[field] = f"generated-{field}-{row_index + 1}"
                rows.append(row)
            generated[table] = pd.DataFrame(rows)
        return generated


class ShortTableGenerator(FakeTableGenerator):
    def generate_for_schemas(self, schemas, prompts=None, sample_sizes=None, **kwargs):
        generated = super().generate_for_schemas(
            schemas, prompts=prompts, sample_sizes=sample_sizes, **kwargs
        )
        first_table = next(iter(generated))
        generated[first_table] = generated[first_table].iloc[:-1]
        return generated


class ReorderingGenerator(FakeTableGenerator):
    def generate_for_schemas(self, schemas, prompts=None, sample_sizes=None, **kwargs):
        generated = super().generate_for_schemas(
            schemas, prompts=prompts, sample_sizes=sample_sizes, **kwargs
        )
        return {
            table: frame.iloc[::-1].reset_index(drop=True)
            for table, frame in generated.items()
        }


class InvalidRowKeyGenerator(FakeTableGenerator):
    def generate_for_schemas(self, schemas, prompts=None, sample_sizes=None, **kwargs):
        generated = super().generate_for_schemas(
            schemas, prompts=prompts, sample_sizes=sample_sizes, **kwargs
        )
        first_table = next(iter(generated))
        generated[first_table].loc[0, "syda_scenario_row_key"] = "invented-row"
        return generated


class UnexpectedTableGenerator:
    def generate_for_schemas(self, *args, **kwargs):
        raise AssertionError("A fully planned scenario must not call the generator")


def _claim_schemas():
    return {
        "Patient": {
            "patient_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "state": "text",
            "registered_at": "date",
        },
        "Policy": {
            "policy_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "patient_id": "foreign_key",
            "effective_at": "date",
            "__foreign_keys__": {"patient_id": "Patient.patient_id"},
        },
        "Diagnosis": {
            "diagnosis_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "patient_id": "foreign_key",
            "diagnosed_at": "date",
            "__foreign_keys__": {"patient_id": "Patient.patient_id"},
        },
        "Claim": {
            "claim_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "policy_id": "foreign_key",
            "diagnosis_id": "foreign_key",
            "filed_at": "datetime",
            "status": "text",
            "patient_state": "text",
            "__foreign_keys__": {
                "policy_id": "Policy.policy_id",
                "diagnosis_id": "Diagnosis.diagnosis_id",
            },
        },
        "Payment": {
            "payment_id": {
                "type": "integer",
                "constraints": {"primary_key": True},
            },
            "claim_id": "foreign_key",
            "paid_at": "datetime",
            "status": "text",
            "__foreign_keys__": {"claim_id": "Claim.claim_id"},
        },
    }


def _claim_steps(payment_cardinality=1):
    return [
        ScenarioStep(
            name="patient",
            table="Patient",
            timestampField="registered_at",
            timeOffsetDays=0,
        ),
        ScenarioStep(
            name="policy",
            table="Policy",
            timestampField="effective_at",
            timeOffsetDays=1,
        ),
        ScenarioStep(
            name="diagnosis",
            table="Diagnosis",
            timestampField="diagnosed_at",
            timeOffsetDays=2,
        ),
        ScenarioStep(
            name="claim",
            table="Claim",
            timestampField="filed_at",
            timeOffsetDays=3,
            values={"status": "submitted"},
        ),
        ScenarioStep(
            name="payment",
            table="Payment",
            recordsPerInstance=payment_cardinality,
            timestampField="paid_at",
            timeOffsetDays=4,
            values={"status": "paid"},
        ),
    ]


def _assert_same_instance_fk(tables, child_table, child_fk, parent_table, parent_pk):
    child = tables[child_table][[child_fk, "scenario_instance_id"]].rename(
        columns={"scenario_instance_id": "child_instance"}
    )
    parent = tables[parent_table][[parent_pk, "scenario_instance_id"]].rename(
        columns={"scenario_instance_id": "parent_instance"}
    )
    joined = child.merge(parent, left_on=child_fk, right_on=parent_pk)
    assert len(joined) == len(child)
    assert (joined["child_instance"] == joined["parent_instance"]).all()


def test_generates_complete_instances_with_shared_context_and_ordered_time():
    generator = FakeTableGenerator()
    definition = ScenarioDefinition(
        name="Claims lifecycle",
        description="A patient claim moving from enrollment to payment.",
        schemas=_claim_schemas(),
        steps=_claim_steps(),
        bindings=[
            ScenarioBinding(
                name="patient state",
                source="Patient.state",
                targets=["Claim.patient_state"],
            )
        ],
    )

    result = ScenarioEngine(generator).generate(
        definition,
        instance_count=3,
        start_at=date(2026, 1, 1),
    )

    assert generator.sample_sizes == {"Patient": 3}
    assert set(generator.schemas["Patient"]) == {
        "syda_scenario_row_key",
        "state",
    }
    assert generator.schemas["Patient"]["syda_scenario_row_key"]["constraints"][
        "enum"
    ] == [
        "claims-lifecycle-000001:patient:0001",
        "claims-lifecycle-000002:patient:0001",
        "claims-lifecycle-000003:patient:0001",
    ]
    assert result.instance_count == 3
    assert result.path_counts == {"default": 3}
    assert set(result.instance_ids) == {
        "claims-lifecycle-000001",
        "claims-lifecycle-000002",
        "claims-lifecycle-000003",
    }
    for frame in result.tables.values():
        assert set(frame["scenario_instance_id"]) == set(result.instance_ids)
        assert set(frame["scenario_path"]) == {"default"}

    _assert_same_instance_fk(
        result.tables, "Policy", "patient_id", "Patient", "patient_id"
    )
    _assert_same_instance_fk(
        result.tables, "Diagnosis", "patient_id", "Patient", "patient_id"
    )
    _assert_same_instance_fk(result.tables, "Claim", "policy_id", "Policy", "policy_id")
    _assert_same_instance_fk(
        result.tables, "Claim", "diagnosis_id", "Diagnosis", "diagnosis_id"
    )
    _assert_same_instance_fk(result.tables, "Payment", "claim_id", "Claim", "claim_id")

    states = result.tables["Patient"].set_index("scenario_instance_id")["state"]
    claim_states = result.tables["Claim"].set_index("scenario_instance_id")[
        "patient_state"
    ]
    pd.testing.assert_series_equal(states, claim_states, check_names=False)

    for instance_id in result.instance_ids:
        timeline = [
            pd.to_datetime(
                result.tables[table]
                .set_index("scenario_instance_id")
                .at[instance_id, field]
            )
            for table, field in (
                ("Patient", "registered_at"),
                ("Policy", "effective_at"),
                ("Diagnosis", "diagnosed_at"),
                ("Claim", "filed_at"),
                ("Payment", "paid_at"),
            )
        ]
        assert timeline == sorted(timeline)
        assert len(set(timeline)) == len(timeline)


def test_plan_is_structural_source_of_truth_before_enrichment():
    generator = FakeTableGenerator()
    engine = ScenarioEngine(generator)
    definition = ScenarioDefinition(
        name="Claims lifecycle",
        schemas=_claim_schemas(),
        steps=_claim_steps(),
    )

    plan = engine.plan(definition, instance_count=2, start_at=date(2026, 1, 1))

    assert generator.sample_sizes == {}
    assert plan.path_assignments == ["default", "default"]
    assert plan.tables["Patient"]["patient_id"].tolist() == [1, 2]
    assert plan.tables["Claim"]["status"].tolist() == ["submitted", "submitted"]
    assert plan.tables["Claim"]["policy_id"].tolist() == [1, 2]
    assert plan.tables["Payment"]["claim_id"].tolist() == [1, 2]
    assert plan.tables["Patient"]["state"].isna().all()

    result = engine.generate(
        definition,
        instance_count=2,
        start_at=date(2026, 1, 1),
    )

    for table, columns in {
        "Patient": ["patient_id", "registered_at"],
        "Claim": ["claim_id", "policy_id", "diagnosis_id", "filed_at", "status"],
        "Payment": ["payment_id", "claim_id", "paid_at", "status"],
    }.items():
        for column in columns:
            pd.testing.assert_series_equal(
                result.tables[table][column],
                plan.tables[table][column],
                check_dtype=False,
            )
    assert result.tables["Patient"]["state"].notna().all()


def test_enrichment_joins_by_row_key_when_provider_reorders_rows():
    definition = ScenarioDefinition(
        name="Reordered customers",
        schemas={
            "Customer": {
                "customer_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "name": "text",
            }
        },
        steps=[ScenarioStep(name="customer", table="Customer")],
    )

    result = ScenarioEngine(ReorderingGenerator()).generate(
        definition,
        instance_count=3,
    )

    assert result.tables["Customer"]["name"].tolist() == [
        "generated-name-1",
        "generated-name-2",
        "generated-name-3",
    ]


def test_later_steps_receive_generated_and_planned_instance_context():
    generator = FakeTableGenerator()
    definition = ScenarioDefinition(
        name="Contextual orders",
        schemas={
            "Customer": {
                "customer_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "name": "text",
            },
            "Order": {
                "order_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "customer_id": "foreign_key",
                "status": "text",
                "description": "text",
                "__foreign_keys__": {"customer_id": "Customer.customer_id"},
            },
        },
        steps=[
            ScenarioStep(name="customer", table="Customer"),
            ScenarioStep(
                name="order",
                table="Order",
                values={"status": "confirmed"},
            ),
        ],
    )

    result = ScenarioEngine(generator).generate(definition, instance_count=2)

    order_prompt = generator.prompts["Order"]
    assert '"scenarioInstanceId": "contextual-orders-000001"' in order_prompt
    assert '"name": "generated-name-1"' in order_prompt
    assert '"status": "confirmed"' in order_prompt
    assert result.tables["Order"]["description"].notna().all()


def test_rejects_provider_rows_without_expected_context_keys():
    definition = ScenarioDefinition(
        name="Invalid provider keys",
        schemas={
            "Customer": {
                "customer_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "name": "text",
            }
        },
        steps=[ScenarioStep(name="customer", table="Customer")],
    )

    with pytest.raises(ValueError, match="invalid row keys"):
        ScenarioEngine(InvalidRowKeyGenerator()).generate(
            definition,
            instance_count=2,
        )


def test_fully_planned_scenario_skips_generator_calls():
    definition = ScenarioDefinition(
        name="Planned events",
        schemas={
            "Event": {
                "event_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "occurred_at": "datetime",
                "status": "text",
            }
        },
        steps=[
            ScenarioStep(
                name="event",
                table="Event",
                timestampField="occurred_at",
                values={"status": "complete"},
            )
        ],
    )

    result = ScenarioEngine(UnexpectedTableGenerator()).generate(
        definition,
        instance_count=2,
        start_at=date(2026, 1, 1),
    )

    assert result.tables["Event"]["event_id"].tolist() == [1, 2]
    assert result.tables["Event"]["status"].tolist() == ["complete", "complete"]


def test_output_skips_tables_excluded_by_every_path(tmp_path):
    definition = ScenarioDefinition(
        name="Parent-only path",
        schemas={
            "Parent": {
                "parent_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                }
            },
            "Child": {
                "child_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "parent_id": "foreign_key",
                "__foreign_keys__": {"parent_id": "Parent.parent_id"},
            },
        },
        steps=[
            ScenarioStep(name="parent", table="Parent"),
            ScenarioStep(name="child", table="Child"),
        ],
        paths=[ScenarioPath(name="parent_only", steps=["parent"])],
    )

    result = ScenarioEngine(UnexpectedTableGenerator()).generate(
        definition,
        instance_count=1,
        output_dir=tmp_path,
    )

    assert len(result.tables["Parent"]) == 1
    assert result.tables["Child"].empty
    assert (tmp_path / "parent.csv").exists()
    assert not (tmp_path / "child.csv").exists()


def test_weighted_paths_create_complete_and_partial_lifecycles():
    definition = ScenarioDefinition(
        name="Claim outcomes",
        schemas=_claim_schemas(),
        steps=_claim_steps(payment_cardinality=2),
        paths=[
            ScenarioPath(
                name="approved",
                steps=["patient", "policy", "diagnosis", "claim", "payment"],
                weight=2,
                overrides={"claim": {"status": "approved"}},
            ),
            ScenarioPath(
                name="denied",
                steps=["patient", "policy", "diagnosis", "claim"],
                weight=1,
                overrides={"claim": {"status": "denied"}},
            ),
            ScenarioPath(
                name="pending",
                steps=["patient", "policy", "diagnosis", "claim"],
                weight=1,
                overrides={"claim": {"status": "pending"}},
            ),
        ],
    )

    result = ScenarioEngine(FakeTableGenerator()).generate(definition, instance_count=8)

    assert result.path_counts == {"approved": 4, "denied": 2, "pending": 2}
    assert {name: len(frame) for name, frame in result.tables.items()} == {
        "Patient": 8,
        "Policy": 8,
        "Diagnosis": 8,
        "Claim": 8,
        "Payment": 8,
    }
    assert set(result.tables["Payment"]["scenario_path"]) == {"approved"}
    assert result.tables["Claim"].groupby("scenario_path")[
        "status"
    ].first().to_dict() == {
        "approved": "approved",
        "denied": "denied",
        "pending": "pending",
    }
    _assert_same_instance_fk(result.tables, "Payment", "claim_id", "Claim", "claim_id")


def test_path_allocation_preserves_exact_weighted_distribution():
    definition = ScenarioDefinition(
        name="Claim outcomes",
        schemas=_claim_schemas(),
        steps=_claim_steps(),
        paths=[
            ScenarioPath(
                name="approved",
                steps=["patient", "policy", "diagnosis", "claim", "payment"],
                weight=0.75,
            ),
            ScenarioPath(
                name="denied",
                steps=["patient", "policy", "diagnosis", "claim"],
                weight=0.15,
            ),
            ScenarioPath(
                name="pending",
                steps=["patient", "policy", "diagnosis", "claim"],
                weight=0.10,
            ),
        ],
    )

    result = ScenarioEngine(FakeTableGenerator()).generate(
        definition, instance_count=100
    )

    assert result.path_counts == {"approved": 75, "denied": 15, "pending": 10}

    summary = ScenarioEngine.summarize(definition, instance_count=1_000_000)
    assert summary.path_counts == {
        "approved": 750_000,
        "denied": 150_000,
        "pending": 100_000,
    }
    assert summary.table_row_counts == {
        "Patient": 1_000_000,
        "Policy": 1_000_000,
        "Diagnosis": 1_000_000,
        "Claim": 1_000_000,
        "Payment": 750_000,
    }


def test_generates_two_hundred_complete_instances_in_bounded_context_batches():
    generator = FakeTableGenerator()
    definition = ScenarioDefinition(
        name="Claim outcomes",
        schemas=_claim_schemas(),
        steps=_claim_steps(),
        paths=[
            ScenarioPath(
                name="approved",
                steps=["patient", "policy", "diagnosis", "claim", "payment"],
                weight=0.75,
                overrides={"claim": {"status": "approved"}},
            ),
            ScenarioPath(
                name="denied",
                steps=["patient", "policy", "diagnosis", "claim"],
                weight=0.15,
                overrides={"claim": {"status": "denied"}},
            ),
            ScenarioPath(
                name="pending",
                steps=["patient", "policy", "diagnosis", "claim"],
                weight=0.10,
                overrides={"claim": {"status": "pending"}},
            ),
        ],
    )

    result = ScenarioEngine(generator).generate(
        definition,
        instance_count=200,
        generation_kwargs={"batch_size": 40},
    )

    assert result.path_counts == {"approved": 150, "denied": 30, "pending": 20}
    assert len(result.tables["Patient"]) == 200
    assert len(result.tables["Claim"]) == 200
    assert len(result.tables["Payment"]) == 150
    assert len(generator.calls) == 10
    assert [next(iter(call["sample_sizes"])) for call in generator.calls] == [
        "Patient",
        "Patient",
        "Patient",
        "Patient",
        "Patient",
        "Claim",
        "Claim",
        "Claim",
        "Claim",
        "Claim",
    ]
    assert all(
        next(iter(call["sample_sizes"].values())) == 40 for call in generator.calls
    )


def test_default_timeline_uses_recent_dates_without_future_events():
    definition = ScenarioDefinition(
        name="Claims lifecycle",
        schemas=_claim_schemas(),
        steps=_claim_steps(),
    )

    result = ScenarioEngine(FakeTableGenerator()).generate(
        definition, instance_count=400
    )

    payment_dates = pd.to_datetime(result.tables["Payment"]["paid_at"])
    assert payment_dates.max().date() <= date.today()
    assert payment_dates.min().date() >= date.today() - pd.Timedelta(days=369)


def test_rejects_path_that_omits_a_required_parent_step():
    with pytest.raises(ValidationError, match="without required parent step 'patient'"):
        ScenarioDefinition(
            name="Broken path",
            schemas=_claim_schemas(),
            steps=_claim_steps(),
            paths=[ScenarioPath(name="broken", steps=["policy"])],
        )


def test_scenario_models_reject_unknown_configuration_fields():
    with pytest.raises(ValidationError, match="recordsPerInstnace"):
        ScenarioStep(
            name="customer",
            table="Customer",
            recordsPerInstnace=2,
        )


def test_rejects_empty_and_duplicate_path_steps():
    with pytest.raises(ValidationError):
        ScenarioPath(name="empty", steps=[])

    with pytest.raises(ValidationError, match="duplicate steps"):
        ScenarioDefinition(
            name="Duplicate path",
            schemas={"Event": {"event_id": "integer"}},
            steps=[ScenarioStep(name="event", table="Event")],
            paths=[ScenarioPath(name="duplicate", steps=["event", "event"])],
        )


def test_rejects_binding_target_path_without_its_source():
    with pytest.raises(ValidationError, match="without source step 'source'"):
        ScenarioDefinition(
            name="Broken binding",
            schemas={
                "Source": {"value": "text"},
                "Target": {"copied": "text"},
            },
            steps=[
                ScenarioStep(name="source", table="Source"),
                ScenarioStep(name="target", table="Target"),
            ],
            paths=[ScenarioPath(name="target_only", steps=["target"])],
            bindings=[
                ScenarioBinding(
                    name="copy",
                    source="Source.value",
                    targets=["Target.copied"],
                )
            ],
        )


def test_rejects_ambiguous_multi_row_binding_source():
    with pytest.raises(ValidationError, match="ambiguous multi-row source"):
        ScenarioDefinition(
            name="Ambiguous binding",
            schemas={
                "Source": {"value": "text"},
                "Target": {"copied": "text"},
            },
            steps=[
                ScenarioStep(
                    name="source",
                    table="Source",
                    recordsPerInstance=2,
                ),
                ScenarioStep(name="target", table="Target"),
            ],
            bindings=[
                ScenarioBinding(
                    name="copy",
                    source="Source.value",
                    targets=["Target.copied"],
                )
            ],
        )


def test_rejects_non_increasing_workflow_timestamps():
    with pytest.raises(ValidationError, match="timestamp offsets must increase"):
        ScenarioDefinition(
            name="Reverse time",
            schemas={
                "First": {"at": "datetime"},
                "Second": {"at": "datetime"},
            },
            steps=[
                ScenarioStep(
                    name="first",
                    table="First",
                    timestampField="at",
                    timeOffsetDays=2,
                ),
                ScenarioStep(
                    name="second",
                    table="Second",
                    timestampField="at",
                    timeOffsetDays=1,
                ),
            ],
        )


def test_structural_validation_rejects_tampered_ledger_fields():
    definition = ScenarioDefinition(
        name="Tamper check",
        schemas={
            "Event": {
                "event_id": {
                    "type": "integer",
                    "constraints": {"primary_key": True},
                },
                "status": "text",
            }
        },
        steps=[
            ScenarioStep(
                name="event",
                table="Event",
                values={"status": "complete"},
            )
        ],
    )
    engine = ScenarioEngine(UnexpectedTableGenerator())
    plan = engine.plan(definition, instance_count=1)
    tables = {name: frame.copy() for name, frame in plan.tables.items()}
    tables["Event"].loc[0, "status"] = "corrupted"

    with pytest.raises(ValueError, match="changed ledger field 'status'"):
        engine._validate_result(definition, plan, tables)


def test_rejects_generator_row_count_mismatch():
    definition = ScenarioDefinition(
        name="Claims lifecycle",
        schemas=_claim_schemas(),
        steps=_claim_steps(),
    )

    with pytest.raises(ValueError, match="expected 2"):
        ScenarioEngine(ShortTableGenerator()).generate(definition, instance_count=2)
