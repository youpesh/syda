import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { LiveConnections, type SchemaSource, useLiveProfiles } from "~/components/studio/live-connections";
import { SchemaExplorer } from "~/components/studio/schema-explorer";
import { Badge } from "~/components/ui/badge";
import { buttonVariants } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Input } from "~/components/ui/input";
import { Spinner } from "~/components/ui/spinner";
import type { GenerationJob, ScenarioConfiguration } from "~/lib/studio-types";

function WorkspaceFrame({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
    <div className="mx-auto max-w-5xl space-y-5">
      <div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1><p className="mt-1 text-sm text-muted-foreground">{description}</p></div>
      {children}
    </div>
  </main>;
}

export function ConnectionsWorkspace({ connections, source, onSource }: {
  connections: ReturnType<typeof useLiveProfiles>;
  source: SchemaSource | null;
  onSource: (value: SchemaSource | null) => void;
}) {
  return <WorkspaceFrame title="Connections" description="Inspect database structure and bring it into this chat.">
    <Card><CardHeader><CardTitle>Database connections</CardTitle><CardDescription>PostgreSQL, MySQL, and Oracle schemas are read through the live backend. Database records are not loaded.</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        <LiveConnections connections={connections} source={source} onSource={onSource} disabled={false} />
        <p className="text-xs text-muted-foreground">Connection profiles stay in this page session. Passwords are not saved.</p>
      </CardContent>
    </Card>
    {source && <Card><CardHeader><CardTitle>{source.name}</CardTitle><CardDescription>{Object.keys(source.schemas).length} tables available to your current conversation.</CardDescription></CardHeader>
      <CardContent className="space-y-4"><SchemaExplorer schemas={source.schemas} /><Link className={buttonVariants({ size: "sm" })} to="?view=scenario">Return to scenario</Link></CardContent>
    </Card>}
  </WorkspaceFrame>;
}

export function SchemaWorkspace({ source, scenario }: { source: SchemaSource | null; scenario?: ScenarioConfiguration }) {
  const schemas = source?.schemas ?? scenario?.schemas;
  const title = source?.name ?? scenario?.title ?? "Current schema";
  return <WorkspaceFrame title="Schema review" description="Review the table structure attached to this conversation.">
    {schemas && Object.keys(schemas).length ? <Card><CardHeader><CardTitle>{title}</CardTitle><CardDescription>{Object.keys(schemas).length} tables · Structure only</CardDescription></CardHeader>
      <CardContent><SchemaExplorer schemas={schemas} /></CardContent>
    </Card> : <Card><CardContent className="space-y-3 py-8"><p className="text-sm text-muted-foreground">No schema is attached to this conversation yet. Connect a database or design a scenario in chat.</p>
      <div className="flex flex-wrap gap-2"><Link className={buttonVariants({ size: "sm" })} to="?view=connections">Open connections</Link><Link className={buttonVariants({ size: "sm", variant: "outline" })} to="?view=scenario">Open scenario</Link></div>
    </CardContent></Card>}
  </WorkspaceFrame>;
}

function statusVariant(status: string): "secondary" | "destructive" | "outline" {
  return status === "complete" ? "secondary" : status === "failed" ? "destructive" : "outline";
}

export function RunHistoryWorkspace() {
  const [jobs, setJobs] = useState<GenerationJob[]>([]);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs?limit=100");
      if (!response.ok) throw new Error(`Could not load runs (${response.status}).`);
      setJobs(await response.json() as GenerationJob[]);
      setError(undefined);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Run history is unavailable.");
    } finally { setLoading(false); }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const filtered = jobs.filter((job) => `${job.scenario?.title ?? ""} ${job.jobId}`.toLowerCase().includes(query.toLowerCase()) && (statusFilter === "all" || job.status === statusFilter));
  return <WorkspaceFrame title="Run history" description="Track real generation jobs and open their outputs.">
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {loading && !jobs.length ? <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Spinner /> Loading runs…</div> : <Card>
      <CardHeader><CardTitle>Generation runs</CardTitle><CardDescription>{jobs.length} recent runs · Refreshes automatically</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        {jobs.length ? <><div className="flex flex-wrap gap-2"><Input aria-label="Search runs" className="max-w-sm" placeholder="Search scenario or run ID" value={query} onChange={(event) => setQuery(event.target.value)} />
          <select aria-label="Filter runs by status" className="h-8 rounded-md border bg-background px-2 text-xs" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All statuses</option><option value="generating">Generating</option><option value="validating">Validating</option><option value="evaluating">Evaluating</option><option value="complete">Complete</option><option value="failed">Failed</option></select></div>
          <div className="divide-y rounded-xl border">{filtered.map((job) => <div className="flex flex-wrap items-center justify-between gap-3 p-4" key={job.jobId}>
            <div className="min-w-0"><p className="truncate text-sm font-medium">{job.scenario?.title ?? `Run ${job.jobId}`}</p><p className="text-xs text-muted-foreground">{job.createdAt ? new Date(job.createdAt).toLocaleString() : job.jobId}</p></div>
            <div className="flex items-center gap-3"><Badge variant={statusVariant(job.status)}>{job.status}</Badge><span className="text-xs tabular-nums text-muted-foreground">{job.progress}%</span><Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/runs/${job.jobId}`}>View run</Link></div>
          </div>)}</div>{!filtered.length && <p className="text-sm text-muted-foreground">No runs match those filters.</p>}</> : <div className="space-y-3"><p className="text-sm text-muted-foreground">No generation runs yet.</p><Link className={buttonVariants({ size: "sm" })} to="?view=scenario">Create a scenario</Link></div>}
      </CardContent>
    </Card>}
  </WorkspaceFrame>;
}
