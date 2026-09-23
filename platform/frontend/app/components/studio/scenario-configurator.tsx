import { useEffect, useState } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  Add01Icon,
  AlertCircleIcon,
  CheckmarkCircle02Icon,
  Delete02Icon,
  WorkflowIcon,
} from "@hugeicons/core-free-icons";

import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldSet, FieldLegend } from "~/components/ui/field";
import { Input } from "~/components/ui/input";
import { Separator } from "~/components/ui/separator";
import { Skeleton } from "~/components/ui/skeleton";
import { Spinner } from "~/components/ui/spinner";
import { SchemaEditor } from "~/components/studio/schema-editor";
import type { CostEstimate, ScenarioConfiguration } from "~/lib/studio-types";

type ValidationResult = {
  valid: boolean;
  evaluated_rules_count: number;
  workflow_steps_count: number;
  evaluated_tables_count: number;
  warnings: string[];
  schema_errors: string[];
};

export function ScenarioConfigurator({
  scenario,
  onChange,
}: {
  scenario: ScenarioConfiguration;
  onChange: (scenario: ScenarioConfiguration) => void;
}) {
  const [estimate, setEstimate] = useState<CostEstimate>();
  const [estimateLoading, setEstimateLoading] = useState(true);
  const [validation, setValidation] = useState<ValidationResult>();
  const [validationLoading, setValidationLoading] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setEstimateLoading(true);
      try {
        const response = await fetch("/api/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scenario }),
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Estimate unavailable");
        setEstimate(await response.json());
      } catch (error) {
        if (!controller.signal.aborted) setEstimate(undefined);
      } finally {
        if (!controller.signal.aborted) setEstimateLoading(false);
      }
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [scenario]);

  const validate = async () => {
    setValidationLoading(true);
    try {
      const response = await fetch("/api/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario }),
      });
      if (!response.ok) throw new Error(`Validation failed (${response.status})`);
      setValidation(await response.json());
    } catch (error) {
      setValidation({
        valid: false,
        evaluated_rules_count: 0,
        workflow_steps_count: 0,
        evaluated_tables_count: 0,
        warnings: [],
        schema_errors: [error instanceof Error ? error.message : "Validation failed"],
      });
    } finally {
      setValidationLoading(false);
    }
  };

  const updateWorkflow = (index: number, value: string) => {
    const workflow = [...scenario.workflow];
    workflow[index] = value;
    onChange({ ...scenario, workflow });
  };

  const updateRule = (index: number, value: string) => {
    const rules = [...scenario.rules];
    rules[index] = value;
    onChange({ ...scenario, rules });
  };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Generation configuration</CardTitle>
          <CardDescription>Review the scenario inputs before any records are generated.</CardDescription>
        </CardHeader>
        <CardContent>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="scenario-title">Scenario name</FieldLabel>
              <Input id="scenario-title" value={scenario.title} onChange={(event) => onChange({ ...scenario, title: event.target.value })} />
            </Field>
            <Field>
              <FieldLabel htmlFor="record-count">Scenario instances</FieldLabel>
              <Input
                id="record-count"
                min={1}
                type="number"
                value={scenario.recordCount}
                onChange={(event) => onChange({ ...scenario, recordCount: Math.max(1, Number(event.target.value) || 1) })}
              />
              <FieldDescription>Each instance follows the full workflow and stays linked across its tables.</FieldDescription>
            </Field>
          </FieldGroup>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Schema preview</CardTitle>
          <CardDescription>Edit field names and types, then validate the schema.</CardDescription>
        </CardHeader>
        <CardContent>
          <SchemaEditor onChange={onChange} scenario={scenario} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Workflow and causal rules</CardTitle>
          <CardDescription>These steps and rules define the lifecycle Syda must preserve.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <FieldSet>
            <FieldLegend>Workflow steps</FieldLegend>
            <FieldGroup className="gap-2">
              {scenario.workflow.map((step, index) => (
                <Field key={`${index}-${step}`} orientation="horizontal">
                  <Badge variant="outline">{index + 1}</Badge>
                  <Input aria-label={`Workflow step ${index + 1}`} value={step} onChange={(event) => updateWorkflow(index, event.target.value)} />
                  <Button aria-label={`Remove ${step}`} disabled={scenario.workflow.length <= 2} onClick={() => onChange({ ...scenario, workflow: scenario.workflow.filter((_, itemIndex) => itemIndex !== index) })} size="icon-sm" type="button" variant="ghost">
                    <HugeiconsIcon icon={Delete02Icon} strokeWidth={2} />
                  </Button>
                </Field>
              ))}
              <Button onClick={() => onChange({ ...scenario, workflow: [...scenario.workflow, `Step ${scenario.workflow.length + 1}`] })} size="sm" type="button" variant="outline">
                <HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} />
                Add workflow step
              </Button>
            </FieldGroup>
          </FieldSet>
          <Separator />
          <FieldSet>
            <FieldLegend>Custom rules</FieldLegend>
            <FieldGroup className="gap-2">
              {scenario.rules.map((rule, index) => (
                <Field key={`${index}-${rule}`} orientation="horizontal">
                  <HugeiconsIcon icon={WorkflowIcon} strokeWidth={2} />
                  <Input aria-label={`Rule ${index + 1}`} value={rule} onChange={(event) => updateRule(index, event.target.value)} />
                  <Button aria-label="Remove rule" onClick={() => onChange({ ...scenario, rules: scenario.rules.filter((_, itemIndex) => itemIndex !== index) })} size="icon-sm" type="button" variant="ghost">
                    <HugeiconsIcon icon={Delete02Icon} strokeWidth={2} />
                  </Button>
                </Field>
              ))}
              <Button onClick={() => onChange({ ...scenario, rules: [...scenario.rules, "Describe the new causal or semantic rule"] })} size="sm" type="button" variant="outline">
                <HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} />
                Add custom rule
              </Button>
            </FieldGroup>
          </FieldSet>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Estimated run cost</CardTitle>
            <CardDescription>Calculated before generation from the selected model and scenario size.</CardDescription>
          </CardHeader>
          <CardContent>
            {estimateLoading ? (
              <div className="flex flex-col gap-2"><Skeleton className="h-5 w-32" /><Skeleton className="h-4 w-full" /></div>
            ) : estimate ? (
              <dl className="grid grid-cols-2 gap-3 text-xs">
                <div><dt className="text-muted-foreground">Provider</dt><dd className="mt-1 font-medium">{estimate.provider}</dd></div>
                <div><dt className="text-muted-foreground">Model</dt><dd className="mt-1 font-medium">{estimate.model}</dd></div>
                <div><dt className="text-muted-foreground">Estimated tokens</dt><dd className="mt-1 font-medium tabular-nums">{(estimate.estimatedInputTokens + estimate.estimatedOutputTokens).toLocaleString()}</dd></div>
                <div><dt className="text-muted-foreground">Estimated cost</dt><dd className="mt-1 font-medium tabular-nums">{estimate.estimatedCostUsd === null ? "Unavailable" : `$${estimate.estimatedCostUsd.toFixed(4)}`}</dd></div>
                <FieldDescription className="col-span-2">{estimate.note}</FieldDescription>
              </dl>
            ) : (
              <Alert variant="destructive"><HugeiconsIcon icon={AlertCircleIcon} strokeWidth={2} /><AlertTitle>Estimate unavailable</AlertTitle><AlertDescription>Check the backend connection before generating.</AlertDescription></Alert>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Preflight validation</CardTitle>
            <CardDescription>Validate tables, relationships, workflow steps, and rule inputs.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {validation && (
              <Alert variant={validation.valid ? "default" : "destructive"}>
                <HugeiconsIcon icon={validation.valid ? CheckmarkCircle02Icon : AlertCircleIcon} strokeWidth={2} />
                <AlertTitle>{validation.valid ? "Configuration is valid" : "Configuration needs attention"}</AlertTitle>
                <AlertDescription>
                  {validation.valid
                    ? `${validation.evaluated_tables_count} tables, ${validation.workflow_steps_count} steps, and ${validation.evaluated_rules_count} rules checked.${validation.warnings.length ? ` ${validation.warnings.join(" ")}` : ""}`
                    : [...validation.schema_errors, ...validation.warnings].join(" ")}
                </AlertDescription>
              </Alert>
            )}
            <Button disabled={validationLoading} onClick={validate} type="button" variant="outline">
              {validationLoading ? <Spinner data-icon="inline-start" /> : <HugeiconsIcon data-icon="inline-start" icon={CheckmarkCircle02Icon} strokeWidth={2} />}
              {validationLoading ? "Validating…" : "Validate configuration"}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
