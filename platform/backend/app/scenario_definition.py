"""Shared translation and validation of platform scenario drafts."""

from typing import Any, Dict

from pydantic import ValidationError
from syda.scenarios import ScenarioDefinition, ScenarioPath, ScenarioStep

from .models.scenario import ScenarioConfiguration


def scenario_definition(
    scenario: ScenarioConfiguration,
    schemas: Dict[str, Any],
) -> ScenarioDefinition:
    """Translate the platform configuration into the reusable core model."""
    ordered_tables = [table for table in scenario.workflow if table in schemas]
    ordered_tables.extend(table for table in schemas if table not in ordered_tables)

    steps = []
    for index, table in enumerate(ordered_tables):
        non_event_dates = {"birth_date", "date_of_birth", "dob"}
        timestamp_field = next(
            (
                field
                for field, definition in schemas[table].items()
                if not field.startswith("__")
                and field.lower() not in non_event_dates
                and (
                    definition.lower()
                    if isinstance(definition, str)
                    else str(definition.get("type", "")).lower()
                )
                in ("date", "datetime")
            ),
            None,
        )
        steps.append(
            ScenarioStep(
                name=table,
                table=table,
                timestampField=timestamp_field,
                timeOffsetDays=index,
            )
        )

    return ScenarioDefinition(
        name=scenario.title,
        description=scenario.description,
        secondaryMetric=scenario.secondary_metric.model_dump(),
        rules=scenario.rules,
        checks=scenario.checks,
        schemas=schemas,
        steps=steps,
        paths=[
            ScenarioPath(
                name=path.name,
                steps=path.steps,
                weight=path.weight,
                overrides=path.overrides,
            )
            for path in scenario.paths
        ],
    )


def scenario_validation_message(error: Exception) -> str:
    """Return actionable validation messages without dumping the scenario payload."""
    if isinstance(error, ValidationError):
        return " ".join(item["msg"].removeprefix("Value error, ") for item in error.errors())
    return str(error)
