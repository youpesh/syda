# Scenario engine

Syda's scenario engine generates complete business lifecycles across related tables. An `instance_count` of 100 means 100 scenario instances—not 100 rows divided among the tables. Each output row includes `scenario_instance_id` and `scenario_path`, so records from one lifecycle can be joined and inspected directly.

The engine layers on `SyntheticDataGenerator` through a plan-first architecture. `ScenarioEngine` first creates a deterministic ledger containing path assignments, row cardinality, primary and foreign keys, fixed statuses, and workflow timestamps. The existing generator then receives only the unconstrained fields and enriches the planned rows with realistic values. Generated values cannot overwrite non-null ledger values.

```text
ScenarioDefinition
        ↓
ScenarioPlan ledger
        ↓
Generate unconstrained fields only
        ↓
Merge enrichment into the ledger
        ↓
Apply shared-field bindings and validate
```

## Example

```python
from datetime import date

from syda import (
    ModelConfig,
    ScenarioDefinition,
    ScenarioEngine,
    ScenarioPath,
    ScenarioStep,
    SyntheticDataGenerator,
)

schemas = {
    "Claim": {
        "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
        "claim_date": "date",
        "status": "text",
    },
    "Payment": {
        "payment_id": {"type": "integer", "constraints": {"primary_key": True}},
        "claim_id": "foreign_key",
        "payment_date": "date",
        "__foreign_keys__": {"claim_id": "Claim.claim_id"},
    },
}

scenario = ScenarioDefinition(
    name="Claim outcomes",
    schemas=schemas,
    steps=[
        ScenarioStep(name="claim", table="Claim", timestampField="claim_date"),
        ScenarioStep(name="payment", table="Payment", timestampField="payment_date"),
    ],
    paths=[
        ScenarioPath(
            name="approved",
            steps=["claim", "payment"],
            weight=0.75,
            overrides={"claim": {"status": "approved"}},
        ),
        ScenarioPath(
            name="denied",
            steps=["claim"],
            weight=0.15,
            overrides={"claim": {"status": "denied"}},
        ),
        ScenarioPath(
            name="pending",
            steps=["claim"],
            weight=0.10,
            overrides={"claim": {"status": "pending"}},
        ),
    ],
)

generator = SyntheticDataGenerator(
    model_config=ModelConfig(provider="openai", model_name="gpt-4o-mini")
)
engine = ScenarioEngine(generator)

# Planning is deterministic and does not call the model.
plan = engine.plan(
    scenario,
    instance_count=100,
    start_at=date(2026, 1, 1),
)
print(plan.path_counts)
print(plan.tables["Claim"].head())

# Large estimates use a count-only summary and do not allocate ledger rows.
summary = ScenarioEngine.summarize(scenario, instance_count=1_000_000)
print(summary.table_row_counts)

result = engine.generate(
    scenario,
    instance_count=100,
    start_at=date(2026, 1, 1),
    output_dir="generated/claims",
)

print(result.tables["Claim"].head())
```

When the batch is large enough to contain every declared path, the allocator guarantees at least one instance of each path and distributes the rest by weight. Allocation is deterministic.

## Shared context and repeated records

Use `ScenarioBinding` to copy a scenario-level value into other tables. For example, `Patient.state` can populate `Claim.patient_state` for the same lifecycle instance. Binding targets are also excluded from model generation because the engine knows they will be populated from their source.

Set `recordsPerInstance` on a step when one lifecycle needs multiple rows of that table, such as two payments or several claim lines. Foreign keys are repaired after field generation so every child points to a parent in the same scenario instance.

Invalid definitions fail before generation when a path references an unknown step, violates workflow order, or includes a child without its required parent.
