import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import type { Route } from "./+types/scenario-editor";
import { AppShell } from "~/components/studio/app-shell";
import { ScenarioConfigurator } from "~/components/studio/scenario-configurator";
import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Spinner } from "~/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "~/components/ui/tabs";
import type { CostEstimate, SavedScenario, ScenarioConfiguration } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) {
  return [{ title: "Scenario editor — Syda" }];
}

const sections = [
  { value: "overview", label: "Overview" },
  { value: "schema", label: "Schema" },
  { value: "workflow", label: "Workflow" },
  { value: "rules", label: "Rules" },
  { value: "checks", label: "Checks & cost" },
] as const;

export default function ScenarioEditor() {
  const { scenarioId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const search = new URLSearchParams(location.search);
  const requestedReturnTo = search.get("returnTo");
  const returnTo = requestedReturnTo?.startsWith("/app") ? requestedReturnTo : undefined;
  const returnMessageId = search.get("messageId") ?? undefined;
  const [saved, setSaved] = useState<SavedScenario>();
  const [draft, setDraft] = useState<ScenarioConfiguration>();
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [estimate, setEstimate] = useState<CostEstimate>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/scenarios/${scenarioId}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 404 ? "Scenario not found." : `Could not load scenario (${response.status}).`);
        return response.json() as Promise<SavedScenario>;
      })
      .then((record) => { setSaved(record); setDraft(record.configuration); setDirty(false); })
      .catch((fetchError) => { if (!controller.signal.aborted) setError(fetchError instanceof Error ? fetchError.message : "Could not load scenario."); });
    return () => controller.abort();
  }, [scenarioId]);

  useEffect(() => {
    if (!draft) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetch("/api/estimate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario: draft }), signal: controller.signal })
        .then((response) => response.ok ? response.json() : undefined)
        .then((value) => { if (!controller.signal.aborted) setEstimate(value); })
        .catch(() => { if (!controller.signal.aborted) setEstimate(undefined); });
    }, 300);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [draft]);

  const updateDraft = (next: ScenarioConfiguration) => { setDraft(next); setDirty(true); setError(undefined); };

  const save = async () => {
    if (!draft || !scenarioId) return;
    setSaving(true);
    setError(undefined);
    try {
      const response = await fetch(`/api/scenarios/${scenarioId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(draft) });
      if (!response.ok) throw new Error(`Could not save scenario (${response.status}).`);
      const record = await response.json() as SavedScenario;
      setSaved(record);
      setDirty(false);
      window.dispatchEvent(new Event("syda:scenarios-changed"));
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save scenario.");
      throw saveError;
    } finally {
      setSaving(false);
    }
  };

  const generate = async () => {
    if (!draft) return;
    setGenerating(true);
    setError(undefined);
    try {
      const validationResponse = await fetch("/api/validate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario: draft }) });
      if (!validationResponse.ok) throw new Error(`Could not validate scenario (${validationResponse.status}).`);
      const validation = await validationResponse.json() as { valid: boolean; schema_errors?: string[] };
      if (!validation.valid) throw new Error(validation.schema_errors?.join(" ") || "Fix the scenario configuration before generating.");
      if (dirty) await save();
      const response = await fetch("/api/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario: draft, scenarioId }) });
      if (!response.ok) {
        const body = await response.json().catch(() => undefined) as { detail?: string } | undefined;
        throw new Error(body?.detail ?? `Could not start generation (${response.status}).`);
      }
      const job = await response.json() as { jobId: string };
      if (returnTo && returnMessageId && scenarioId) {
        navigate(returnTo, {
          state: {
            editedScenario: draft,
            messageId: returnMessageId,
            generatedRun: {
              messageId: returnMessageId,
              jobId: job.jobId,
              scenarioId,
              scenario: draft,
            },
          },
        });
      } else {
        navigate(`/runs/${job.jobId}`);
      }
    } catch (generationError) {
      setError(generationError instanceof Error ? generationError.message : "Could not start generation.");
    } finally {
      setGenerating(false);
    }
  };

  const returnToChat = async () => {
    if (!returnTo) return;
    try {
      if (dirty) await save();
      navigate(returnTo, {
        state: draft && returnMessageId ? { editedScenario: draft, messageId: returnMessageId } : null,
      });
    } catch {
      // Keep the editor open so the user can correct or retry a failed save.
    }
  };

  return <AppShell title={draft?.title ?? "Scenario editor"} subtitle="Review the configuration before generation" headerActions={draft && <Button disabled={!dirty || saving} onClick={() => void save().catch(() => {})} size="sm" variant="outline">{saving ? <Spinner data-icon="inline-start" /> : null}{dirty ? "Save changes" : "Saved"}</Button>}>
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
      <div className="mx-auto max-w-7xl space-y-5">
        {returnTo ? (
          <Button disabled={saving || generating} onClick={() => void returnToChat()} size="sm" type="button" variant="ghost">
            {dirty ? "Save and return to chat" : "Return to chat"}
          </Button>
        ) : (
          <Link className="text-xs text-muted-foreground hover:underline" to="/scenarios">← All scenarios</Link>
        )}
        {error && <Alert variant="destructive"><AlertTitle>Scenario needs attention</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
        {!draft ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">{error ? "" : <><Spinner /> Loading scenario…</>}</div> : <>
          <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-2xl font-semibold tracking-tight">{draft.title}</h1><p className="mt-1 max-w-3xl text-sm text-muted-foreground">{draft.description}</p></div><Badge variant="outline">{dirty ? "Unsaved changes" : "Saved scenario"}</Badge></div>
          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
            <Tabs defaultValue="overview" className="min-w-0">
              <TabsList className="max-w-full flex-wrap" variant="line">{sections.map((section) => <TabsTrigger key={section.value} value={section.value}>{section.label}</TabsTrigger>)}</TabsList>
              {sections.map((section) => <TabsContent className="pt-3" key={section.value} value={section.value}><ScenarioConfigurator onChange={updateDraft} scenario={draft} section={section.value} /></TabsContent>)}
            </Tabs>
            <Card className="xl:sticky xl:top-0">
              <CardHeader><CardTitle>Ready to generate?</CardTitle><CardDescription>Syda will use this saved scenario as a snapshot for the run.</CardDescription></CardHeader>
              <CardContent className="space-y-4">
                <dl className="space-y-2 text-xs"><div className="flex justify-between gap-3"><dt className="text-muted-foreground">Instances</dt><dd className="font-medium tabular-nums">{draft.recordCount.toLocaleString()}</dd></div><div className="flex justify-between gap-3"><dt className="text-muted-foreground">Tables</dt><dd className="font-medium tabular-nums">{Object.keys(draft.schemas ?? {}).length}</dd></div><div className="flex justify-between gap-3"><dt className="text-muted-foreground">Workflow steps</dt><dd className="font-medium tabular-nums">{draft.workflow.length}</dd></div><div className="flex justify-between gap-3"><dt className="text-muted-foreground">Estimated cost</dt><dd className="font-medium tabular-nums">{estimate?.estimatedCostUsd == null ? "Unavailable" : `$${estimate.estimatedCostUsd.toFixed(4)}`}</dd></div></dl>
                {estimate?.note && <p className="text-xs text-muted-foreground">{estimate.note}</p>}
                <Button className="w-full" disabled={generating || saving} onClick={() => void generate()}>{generating ? <Spinner data-icon="inline-start" /> : null}{generating ? "Starting run…" : "Generate dataset"}</Button>
                {saved && <p className="text-[10px] text-muted-foreground">Scenario ID {saved.id}</p>}
              </CardContent>
            </Card>
          </div>
        </>}
      </div>
    </main>
  </AppShell>;
}
