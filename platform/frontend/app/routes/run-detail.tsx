import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import type { Route } from "./+types/run-detail";
import { AppShell } from "~/components/studio/app-shell";
import { DataPreview } from "~/components/studio/data-preview";
import { EvaluationReport } from "~/components/studio/evaluation-report";
import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Badge } from "~/components/ui/badge";
import { Button, buttonVariants } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Progress, ProgressLabel, ProgressValue } from "~/components/ui/progress";
import { Spinner } from "~/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "~/components/ui/tabs";
import type { GenerationJob } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) { return [{ title: "Generation run — Syda" }]; }

function statusVariant(status: string): "secondary" | "destructive" | "outline" {
  return status === "complete" ? "secondary" : status === "failed" ? "destructive" : "outline";
}

export default function RunDetail() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [job, setJob] = useState<GenerationJob>();
  const [error, setError] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [savingScenario, setSavingScenario] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      if (!response.ok) throw new Error(response.status === 404 ? "Run not found." : `Could not load run (${response.status}).`);
      setJob(await response.json());
      setError(undefined);
    } catch (fetchError) {
      setError(fetchError instanceof Error ? fetchError.message : "Could not load run.");
    }
  }, [jobId]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const complete = job?.status === "complete";
  const filesAvailable = complete && job.filesAvailable;
  const active = job && !["complete", "failed"].includes(job.status);
  const requestedTab = searchParams.get("tab");
  const activeTab = requestedTab === "data" || requestedTab === "evaluation" ? requestedTab : "overview";

  const saveScenario = async () => {
    if (!job?.scenario) return;
    setSavingScenario(true);
    setActionError(undefined);
    try {
      const response = await fetch("/api/scenarios", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(job.scenario) });
      if (!response.ok) throw new Error(`Could not save scenario (${response.status}).`);
      const saved = await response.json() as { id: string };
      navigate(`/scenarios/${saved.id}`);
    } catch (saveError) {
      setActionError(saveError instanceof Error ? saveError.message : "Could not save scenario.");
    } finally { setSavingScenario(false); }
  };

  return <AppShell title={job?.scenario?.title ?? "Generation run"} subtitle={job ? `Run ${job.jobId}` : "Loading run"} headerActions={job && <div className="flex items-center gap-2">{job.scenarioId ? <Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/scenarios/${job.scenarioId}`}>Edit scenario</Link> : <Button disabled={savingScenario} onClick={() => void saveScenario()} size="sm" variant="outline">{savingScenario ? "Saving…" : "Save as scenario"}</Button>}{filesAvailable && <a className={buttonVariants({ size: "sm" })} href={job.downloadUrl ?? `/api/download/${job.jobId}`}>Download dataset</a>}</div>}>
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8"><div className="mx-auto max-w-6xl space-y-5">
      <Link className="text-xs text-muted-foreground hover:underline" to="/history">← Generation history</Link>
      {error && <Alert variant="destructive"><AlertTitle>Run unavailable</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
      {actionError && <Alert variant="destructive"><AlertTitle>Could not save scenario</AlertTitle><AlertDescription>{actionError}</AlertDescription></Alert>}
      {!job ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">{error ? "" : <><Spinner /> Loading run…</>}</div> : <>
        <div className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-2xl font-semibold tracking-tight">{job.scenario?.title ?? `Run ${job.jobId}`}</h1><p className="mt-1 text-sm text-muted-foreground">{job.scenario?.description}</p><p className="mt-1 text-xs text-muted-foreground">Run {job.jobId} · {job.createdAt ? new Date(job.createdAt).toLocaleString() : "Date unavailable"}</p></div><Badge variant={statusVariant(job.status)}>{job.status}</Badge></div>
        {active && <Card><CardContent><Progress value={job.progress}><ProgressLabel>{job.currentStage}</ProgressLabel><ProgressValue>{(value) => value}</ProgressValue></Progress></CardContent></Card>}
        {job.status === "failed" && <Alert variant="destructive"><AlertTitle>Generation failed</AlertTitle><AlertDescription>{job.currentStage}</AlertDescription></Alert>}
        {complete && !filesAvailable && <Alert><AlertTitle>Dataset files unavailable</AlertTitle><AlertDescription>The run record and evaluation remain available, but its generated files were not retained by the earlier container setup.</AlertDescription></Alert>}
        <Tabs value={activeTab} onValueChange={(value) => setSearchParams(value === "overview" ? {} : { tab: String(value) }, { preventScrollReset: true })}>
          <TabsList variant="line"><TabsTrigger value="overview">Overview</TabsTrigger><TabsTrigger disabled={!filesAvailable} value="data">Generated data</TabsTrigger><TabsTrigger disabled={!complete} value="evaluation">Evaluation</TabsTrigger></TabsList>
          <TabsContent className="pt-3" value="overview"><div className="grid gap-4 lg:grid-cols-2">
            <Card><CardHeader><CardTitle>Run summary</CardTitle><CardDescription>Configuration captured when this run started.</CardDescription></CardHeader><CardContent><dl className="grid gap-3 text-xs sm:grid-cols-2"><div><dt className="text-muted-foreground">Requested instances</dt><dd className="mt-1 font-medium tabular-nums">{job.scenario?.recordCount?.toLocaleString() ?? "—"}</dd></div><div><dt className="text-muted-foreground">Records generated</dt><dd className="mt-1 font-medium tabular-nums">{job.stats?.recordsGenerated?.toLocaleString() ?? "—"}</dd></div><div><dt className="text-muted-foreground">Tables</dt><dd className="mt-1 font-medium tabular-nums">{Object.keys(job.scenario?.schemas ?? {}).length}</dd></div><div><dt className="text-muted-foreground">Duration</dt><dd className="mt-1 font-medium tabular-nums">{job.stats ? `${job.stats.durationSeconds}s` : "—"}</dd></div></dl></CardContent></Card>
            <Card><CardHeader><CardTitle>Workflow</CardTitle><CardDescription>The causal order used for generation.</CardDescription></CardHeader><CardContent><div className="flex flex-wrap gap-2">{job.scenario?.workflow?.map((step, index) => <Badge key={`${step}-${index}`} variant="outline">{index + 1}. {step}</Badge>)}</div>{job.scenario?.paths?.length ? <p className="mt-4 text-xs text-muted-foreground">{job.scenario.paths.length} scenario paths configured</p> : null}</CardContent></Card>
            <Card className="lg:col-span-2"><CardHeader><CardTitle>Rules</CardTitle><CardDescription>Constraints included in this run.</CardDescription></CardHeader><CardContent><ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">{job.scenario?.rules?.map((rule, index) => <li key={index}>{rule}</li>)}</ul></CardContent></Card>
          </div></TabsContent>
          <TabsContent className="pt-3" value="data"><DataPreview complete={complete} jobId={job.jobId} /></TabsContent>
          <TabsContent className="pt-3" value="evaluation"><EvaluationReport jobId={job.jobId} stats={job.stats} /></TabsContent>
        </Tabs>
      </>}
    </div></main>
  </AppShell>;
}
