import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";

import type { Route } from "./+types/history";
import { AppShell } from "~/components/studio/app-shell";
import { Badge } from "~/components/ui/badge";
import { buttonVariants } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "~/components/ui/empty";
import { Input } from "~/components/ui/input";
import { Spinner } from "~/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import type { GenerationJob } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) { return [{ title: "Generation history — Syda" }]; }

function statusVariant(status: string): "secondary" | "destructive" | "outline" {
  return status === "complete" ? "secondary" : status === "failed" ? "destructive" : "outline";
}

export default function GenerationHistory() {
  const [jobs, setJobs] = useState<GenerationJob[]>([]);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/jobs?limit=100");
      if (!response.ok) throw new Error(`Could not load history (${response.status}).`);
      setJobs(await response.json());
      setError(undefined);
    } catch (fetchError) {
      setError(fetchError instanceof Error ? fetchError.message : "Generation history is unavailable.");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const filtered = jobs.filter((job) => `${job.scenario?.title ?? ""} ${job.jobId}`.toLowerCase().includes(query.toLowerCase()) && (statusFilter === "all" || job.status === statusFilter));

  return <AppShell title="Generation history" subtitle="Past runs and generated outputs">
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8"><div className="mx-auto max-w-6xl space-y-5">
      <div><h1 className="text-2xl font-semibold tracking-tight">Generation history</h1><p className="mt-1 text-sm text-muted-foreground">Inspect progress, outputs, and evaluation for each run.</p></div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {loading && jobs.length === 0 ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground"><Spinner /> Loading runs…</div> : jobs.length === 0 ? (
        <Card><CardContent><Empty><EmptyHeader><EmptyTitle>No generation runs yet</EmptyTitle><EmptyDescription>Generate a dataset from a scenario to see its run here.</EmptyDescription></EmptyHeader><Link className={buttonVariants({})} to="/app">Create a scenario</Link></Empty></CardContent></Card>
      ) : <Card>
        <CardHeader><CardTitle>Runs</CardTitle><CardDescription>{jobs.length} most recent · refreshes automatically</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap gap-2"><Input aria-label="Search runs" className="max-w-sm" onChange={(event) => setQuery(event.target.value)} placeholder="Search scenario or run ID" value={query} /><select aria-label="Filter by status" className="h-8 border bg-background px-2 text-xs" onChange={(event) => setStatusFilter(event.target.value)} value={statusFilter}><option value="all">All statuses</option><option value="generating">Generating</option><option value="validating">Validating</option><option value="evaluating">Evaluating</option><option value="complete">Complete</option><option value="failed">Failed</option></select></div>
          <Table><TableHeader><TableRow><TableHead>Scenario</TableHead><TableHead>Status</TableHead><TableHead className="hidden sm:table-cell">Progress</TableHead><TableHead className="hidden md:table-cell">Created</TableHead><TableHead /></TableRow></TableHeader>
            <TableBody>{filtered.map((job) => <TableRow key={job.jobId}>
              <TableCell className="min-w-0 whitespace-normal"><Link className="font-medium hover:underline" to={`/runs/${job.jobId}`}>{job.scenario?.title ?? `Run ${job.jobId}`}</Link><span className="mt-1 block text-[10px] text-muted-foreground">{job.jobId}</span></TableCell>
              <TableCell><Badge variant={statusVariant(job.status)}>{job.status}</Badge></TableCell>
              <TableCell className="hidden tabular-nums sm:table-cell">{job.progress}%</TableCell>
              <TableCell className="hidden text-muted-foreground md:table-cell">{job.createdAt ? new Date(job.createdAt).toLocaleString() : "—"}</TableCell>
              <TableCell><Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/runs/${job.jobId}`}>View run</Link></TableCell>
            </TableRow>)}</TableBody>
          </Table>
          {filtered.length === 0 && <p className="py-6 text-center text-xs text-muted-foreground">No runs match those filters.</p>}
        </CardContent>
      </Card>}
    </div></main>
  </AppShell>;
}
