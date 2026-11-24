# Scenario engine

Syda's scenario engine generates complete business lifecycles across related tables. An `instance_count` of 100 means 100 scenario instances—not 100 rows divided among the tables. Each output row includes `scenario_instance_id` and `scenario_path`, so records from one lifecycle can be joined and inspected directly.

The engine layers on `SyntheticDataGenerator` through a plan-first architecture. `ScenarioEngine` first creates a deterministic ledger containing path assignments, row cardinality, primary and foreign keys, fixed statuses, and workflow timestamps. The existing generator then receives only the unconstrained fields and enriches the planned rows with realistic values. Each enrichment request includes the current ledger row and known records from earlier workflow steps, so generated values can remain consistent with the same scenario instance. Generated values cannot overwrite non-null ledger values.

```text
ScenarioDefinition
        ↓
ScenarioPlan ledger
        ↓
Generate unconstrained fields with per-instance context
        ↓
Join enrichment to the ledger by an internal row key
        ↓
Apply shared-field bindings
        ↓
Validate structural invariants
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

## Enrichment safety

The provider receives a temporary row key with every row context and must return that key with the generated fields. The engine validates that every expected key appears exactly once and joins enrichment by key rather than response position. The temporary key is removed from final output.

Generation proceeds in workflow order. A later step can therefore use both deterministic ledger values and generated values from earlier steps as context. Context is split using the configured generation `batch_size` (50 rows by default) to keep requests bounded.

After enrichment, the engine verifies row counts, ledger-owned values, primary-key uniqueness, same-instance foreign keys, bindings, path membership, and timestamp order before returning or saving output.

Scenario models reject unknown configuration fields. Paths cannot be empty or repeat a step, timestamp offsets must increase with workflow order, and a binding source must appear before its targets on every applicable path. Bindings with multi-row sources are rejected as ambiguous; model those with an explicit aggregation or selection step instead.

Primary keys, referenced parent identities, child foreign keys, and workflow
timestamps are owned by the structural ledger. Fixed step values, path overrides,
and binding targets cannot write to those fields. Referenced parent identities must
also remain non-null and globally unique so every child foreign key identifies
exactly one parent row in the same scenario instance.

## Causal validation and rejected datasets

Declare executable checks in `ScenarioDefinition.checks`. Free-text `rules` provide
generation context; they are not a substitute for executable checks. Use explicit
paths and overrides when you need specific branch outcomes and rates.

- `allowed_values` checks a field against its configured values.
- `temporal_order` checks a `before`/`after` pair or a `chain`. Comparisons within
  one table apply to each record independently. Across tables, ordering applies
  within each scenario instance: the earlier step's maximum timestamp must not
  exceed the later step's minimum. Set `strict=True` to forbid equal timestamps.
- `absence_by_value` forbids a table's records for instances matching the source
  value, including Boolean and numeric values.
- `state_machine` / `state_transition` validate declared transitions. Use
  `previous_field` for row-level transitions, or `order_by` for a status history
  grouped by scenario instance. Histories check their declared initial and
  terminal states. If `order_by` is omitted, the step's timestamp field is used
  when available, otherwise the input row order is used.
- `semantic_invariant` checks an expression or a condition/assertion pair;
  `custom_rule` checks an expression. Expressions use a restricted interpreter,
  not Python `eval`. Evaluation errors are violations, not successful checks.

Malformed non-null timestamps are violations. Temporal comparisons skip null timestamps. When a timestamp must be present,
declare an additional custom rule such as `not is_none(submitted_at)`. Missing
tables or columns in declared checks are reported as violations. A table omitted
by a path remains an empty frame with its schema columns, so an intentionally
absent step does not require invented records.

Generation runs causal validation automatically after enrichment and structural
validation. Failed checks raise `ScenarioValidationError` **before saving output**:

```python
from syda.scenarios import ScenarioValidationError

try:
    result = engine.generate(scenario, instance_count=200, output_dir="output")
except ScenarioValidationError as error:
    for violation in error.violations:
        print(violation.table, violation.row, violation.rule, violation.error)
```

`error.result` contains the rejected tables for diagnostics; it is not an accepted
dataset. To inspect existing data without generating or exporting anything, use
`ScenarioEngine.validate_causal_workflow(definition, tables)` to obtain check
results and structured violations.

The platform persists failed evaluations as failed jobs, retains their JSON/HTML
reports at `/api/evaluation/{job_id}/report`, and blocks dataset previews and
downloads. Rejected data is not exported as CSV. The run's Evaluation tab remains
available so users can inspect the rule, table, instance, and reason before retrying.

The platform also verifies scenario path coverage against the deterministic plan.
`pathCounts` remains the planned allocation; `pathCoverage` in job stats and the
evaluation report lists each path's expected and observed instance counts, status,
and any missing or extra step rows. The observed counts come from generated
`scenario_instance_id` and `scenario_path` values. A mismatch fails the quality
gate and appears in the violation report. Runs without those tracking fields
report path coverage as `not_evaluated`.

### Schema semantic checks in the platform

The platform's semantic correctness metric audits the generated values against
declared field constraints before publishing dataset files:

- `not_null: true` or `nullable: false` rejects null values. Primary keys are
  also required and unique.
- `unique: true` flags every row sharing a non-null duplicate value. Nullable
  unique fields may contain multiple nulls. Numeric, boolean, and temporal
  values are compared in their declared type: integer `"01"` and `1` are
  duplicates, as are equivalent timestamps with different UTC offsets.
- `enum` restricts non-null values to the supplied list. Use it for allowed
  statuses or a bounded reference code list, for example:

```yaml
diagnosis_code_icd10:
  type: text
  constraints:
    not_null: true
    enum: ["E11.9", "I10"]
```

- `min` and `max` check numeric or date/datetime bounds, including their
  endpoints. Non-null values that cannot be parsed for their field type fail
  each declared bound. Datetimes are compared in UTC; date fields compare days.

The example list is a small test fixture; Syda does not supply or verify a
complete official medical code catalog. Callers supply the reference values.
Nested `constraints` override equivalent field-level shorthand declarations.
Boolean constraints accept standard boolean representations, such as `true`,
`"true"`, and `1`, and are normalized consistently for generation and evaluation.
Invalid boolean values, malformed or empty enum lists, invalid bounds, and
reversed bounds are rejected before a generation job is created.
Numeric enum lists are canonicalized and deduplicated using their field type.
Integer bounds retain exact integer precision, including values above `2**53`.
Fractional integer bounds are rounded inward (`min: 1.2`, `max: 3.8` becomes
`2..3`); a range containing no integer is rejected. Declared `decimals` must
normalize to an integer from 0 through 324. Canonical bounds and precision are
passed to generation so accepted numeric strings do not cause conversion errors.
Empty branch tables are handled by scenario coverage; a missing constrained
column in a populated table fails semantic evaluation.

Violations appear in existing JSON/HTML reports and the Evaluation tab with the
table, instance (or row index), field, and constraint, such as
`schema.diagnosis_code_icd10.enum`. Counts represent failed cell checks, so a
single row can contribute multiple violations. Evaluation never modifies data.
Schema diagnostics are limited to the first 1,000 entries across all fields.
The semantic metric keeps the exact violation total and includes
`omittedViolations` and explanatory text when more diagnostics were omitted.

Offline generation keeps counters and unique value pools on each generator
instance, scoped by table and field, across scenario enrichment batches.
Separate runs start with fresh state. Failed calls do not consume values or
advance counters. Unique numeric and enum pools fail with an exhaustion error
when no distinct values remain; they never wrap around and reuse earlier values.
Floating pools respect an explicit decimal precision, or choose precision from
the bounds when none is specified. Offline unique allocation supports numeric,
boolean, text, and email fields; unsupported unique field types produce an
explicit error. The final evaluation gate still checks provider output before
any dataset files are published.

Built-in previews, JSON downloads, database exports, and dataset queries read
CSV floats with round-trip precision so distinct validated values remain
distinct. When reading CSV files directly with pandas, use
`pd.read_csv(path, float_precision="round_trip")` to preserve the same values.
