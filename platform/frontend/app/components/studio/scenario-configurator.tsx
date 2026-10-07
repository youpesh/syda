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
import { Skeleton } from "~/components/ui/skeleton";
import { Spinner } from "~/components/ui/spinner";
import { Textarea } from "~/components/ui/textarea";
import { SchemaEditor } from "~/components/studio/schema-editor";
import type { CostEstimate, ScenarioConfiguration } from "~/lib/studio-types";

type ValidationResult = {
  valid: boolean;
  executable_checks_count: number;
  guidance_rules_count: number;
  workflow_steps_count: number;
  evaluated_tables_count: number;
  warnings: string[];
  schema_errors: string[];
};

type ScenarioPath = NonNullable<ScenarioConfiguration["paths"]>[number];

function PathStepsEditor({ value, index, onChange }: { value: string[]; index: number; onChange: (next: string[]) => void }) {
  const [text, setText] = useState(value.join(", "));
  useEffect(() => { setText(value.join(", ")); }, [value]);
  return <Field><FieldLabel>Steps in this path</FieldLabel><Input aria-label={`Path ${index + 1} steps`} onBlur={() => onChange(text.split(",").map((step) => step.trim()).filter(Boolean))} onChange={(event) => setText(event.target.value)} value={text} /><FieldDescription>Comma-separated workflow step names, in order.</FieldDescription></Field>;
}

function PathWeightEditor({ value, index, onChange }: { value: number; index: number; onChange: (next: number) => void }) {
  const [text, setText] = useState(String(Number(value.toFixed(4))));
  useEffect(() => { setText(String(Number(value.toFixed(4)))); }, [value]);
  return <Input aria-label={`Path ${index + 1} weight`} min={0.001} onBlur={() => { const parsed = Number(text); onChange(Number.isFinite(parsed) && parsed > 0 ? parsed : value); }} onChange={(event) => setText(event.target.value)} step="0.01" type="number" value={text} />;
}

function PathOverridesEditor({ value, onChange }: { value: ScenarioPath["overrides"]; onChange: (next: ScenarioPath["overrides"]) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [error, setError] = useState<string>();

  useEffect(() => { setText(JSON.stringify(value, null, 2)); }, [value]);

  const apply = () => {
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Use a JSON object keyed by workflow step.");
      onChange(parsed);
      setError(undefined);
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "Invalid JSON.");
    }
  };

  return <div className="space-y-1"><label className="text-xs font-medium">Step overrides (JSON)</label><Textarea aria-label="Step overrides JSON" className="min-h-24 font-mono text-xs" onBlur={apply} onChange={(event) => setText(event.target.value)} value={text} />{error && <p className="text-xs text-destructive">{error}</p>}</div>;
}

export function ScenarioConfigurator({
  scenario,
  onChange,
  section = "all",
}: {
  scenario: ScenarioConfiguration;
  onChange: (scenario: ScenarioConfiguration) => void;
  section?: "all" | "overview" | "schema" | "workflow" | "rules" | "checks";
}) {
  const [estimate, setEstimate] = useState<CostEstimate>();
  const [estimateLoading, setEstimateLoading] = useState(true);
  const [validation, setValidation] = useState<ValidationResult>();
  const [validationLoading, setValidationLoading] = useState(false);
  const [checksText, setChecksText] = useState(JSON.stringify(scenario.checks ?? [], null, 2));
  const [checksError, setChecksError] = useState<string>();

  useEffect(() => {
    setChecksText(JSON.stringify(scenario.checks ?? [], null, 2));
    setChecksError(undefined);
  }, [scenario.checks]);

  useEffect(() => {
    if (section !== "all" && section !== "checks") return;
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
  }, [scenario, section]);

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
        executable_checks_count: 0,
        guidance_rules_count: 0,
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

  const applyChecks = () => {
    try {
      const parsed: unknown = JSON.parse(checksText);
      if (!Array.isArray(parsed)) throw new Error("Checks must be a JSON array.");
      onChange({ ...scenario, checks: parsed as NonNullable<ScenarioConfiguration["checks"]> });
      setChecksError(undefined);
    } catch (error) {
      setChecksError(error instanceof Error ? error.message : "Invalid JSON.");
    }
  };

  const updatePath = (index: number, changes: Partial<ScenarioPath>) => {
    const paths = [...(scenario.paths ?? [])];
    paths[index] = { ...paths[index], ...changes };
    onChange({ ...scenario, paths });
  };

  return (
    <div className="flex flex-col gap-4">
      {(section === "all" || section === "overview") && <Card>
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
              <FieldLabel htmlFor="scenario-description">Description</FieldLabel>
              <Textarea id="scenario-description" value={scenario.description} onChange={(event) => onChange({ ...scenario, description: event.target.value })} />
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
            <div className="grid gap-3 sm:grid-cols-2">
              <Field><FieldLabel htmlFor="metric-label">Key metric</FieldLabel><Input id="metric-label" value={scenario.secondaryMetric?.label ?? ""} onChange={(event) => onChange({ ...scenario, secondaryMetric: { ...scenario.secondaryMetric, label: event.target.value } })} /></Field>
              <Field><FieldLabel htmlFor="metric-value">Target value</FieldLabel><Input id="metric-value" value={scenario.secondaryMetric?.value ?? ""} onChange={(event) => onChange({ ...scenario, secondaryMetric: { ...scenario.secondaryMetric, value: event.target.value } })} /></Field>
            </div>
          </FieldGroup>
        </CardContent>
      </Card>}

      {(section === "all" || section === "schema") && <Card>
        <CardHeader>
          <CardTitle>Schema preview</CardTitle>
          <CardDescription>Edit field names and types, then validate the schema.</CardDescription>
        </CardHeader>
        <CardContent>
          <SchemaEditor onChange={onChange} scenario={scenario} />
        </CardContent>
      </Card>}

      {(section === "all" || section === "workflow") && <Card>
        <CardHeader>
          <CardTitle>Workflow</CardTitle>
          <CardDescription>Define the order of records in each scenario instance.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <FieldSet>
            <FieldLegend>Workflow steps</FieldLegend>
            <FieldGroup className="gap-2">
              {scenario.workflow.map((step, index) => (
                <Field key={index} orientation="horizontal">
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
          <FieldSet>
            <FieldLegend>Scenario paths</FieldLegend>
            <FieldDescription>Use weighted branches for normal and edge cases. Leave empty to use the full workflow for every instance.</FieldDescription>
            <div className="mt-3 space-y-3">{(scenario.paths ?? []).map((path, index) => <div className="space-y-3 border p-3" key={index}>
              <div className="grid gap-2 sm:grid-cols-[1fr_110px_auto]"><Input aria-label={`Path ${index + 1} name`} onChange={(event) => updatePath(index, { name: event.target.value })} value={path.name} /><PathWeightEditor index={index} onChange={(weight) => updatePath(index, { weight })} value={path.weight} /><Button aria-label={`Remove path ${path.name}`} onClick={() => onChange({ ...scenario, paths: (scenario.paths ?? []).filter((_, pathIndex) => pathIndex !== index) })} size="icon-sm" type="button" variant="ghost"><HugeiconsIcon icon={Delete02Icon} strokeWidth={2} /></Button></div>
              <PathStepsEditor index={index} onChange={(steps) => updatePath(index, { steps })} value={path.steps} />
              <PathOverridesEditor onChange={(overrides) => updatePath(index, { overrides })} value={path.overrides} />
            </div>)}</div>
            <Button onClick={() => onChange({ ...scenario, paths: [...(scenario.paths ?? []), { name: `path_${(scenario.paths?.length ?? 0) + 1}`, steps: [...scenario.workflow], weight: 1, overrides: {} }] })} size="sm" type="button" variant="outline"><HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} />Add path</Button>
          </FieldSet>
        </CardContent>
      </Card>}

      {(section === "all" || section === "rules") && <Card>
        <CardHeader><CardTitle>Causal rules</CardTitle><CardDescription>Describe business rules and mark the rules Syda can check against generated rows.</CardDescription></CardHeader>
        <CardContent>
          <FieldSet>
            <FieldLegend>Custom rules</FieldLegend>
            <FieldGroup className="gap-2">
              {scenario.rules.map((rule, index) => (
                <Field key={index} orientation="horizontal">
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
          <FieldSet className="mt-5">
            <FieldLegend>Executable checks</FieldLegend>
            <FieldDescription>Supported checks: allowed field values, timestamp order between two Table.field values, and a table’s absence when a source field has a chosen value. Free-text rules without a matching check are guidance only.</FieldDescription>
            <Textarea aria-label="Executable checks JSON" className="min-h-36 font-mono text-xs" onBlur={applyChecks} onChange={(event) => setChecksText(event.target.value)} value={checksText} />
            {checksError && <p className="text-xs text-destructive">{checksError}</p>}
          </FieldSet>
        </CardContent>
      </Card>}

      {(section === "all" || section === "checks") && <div className="grid gap-4 lg:grid-cols-2">
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
                    ? `${validation.evaluated_tables_count} tables and ${validation.workflow_steps_count} steps validated; ${validation.executable_checks_count} executable checks are defined. ${validation.guidance_rules_count} free-text rules are guidance, not checked against rows.${validation.warnings.length ? ` ${validation.warnings.join(" ")}` : ""}`
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
      </div>}
    </div>
  );
}
