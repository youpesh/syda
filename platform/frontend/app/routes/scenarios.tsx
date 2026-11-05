import { useEffect, useState } from "react";
import { Link } from "react-router";

import type { Route } from "./+types/scenarios";
import { AppShell } from "~/components/studio/app-shell";
import { Alert, AlertDescription } from "~/components/ui/alert";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "~/components/ui/dialog";
import { Button, buttonVariants } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "~/components/ui/empty";
import { Spinner } from "~/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import type { SavedScenario } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) {
  return [{ title: "Scenarios — Syda" }];
}

function DeleteScenarioButton({ scenario, onDeleted }: { scenario: SavedScenario; onDeleted: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string>();

  const deleteScenario = async () => {
    setDeleting(true);
    setError(undefined);
    try {
      const response = await fetch(`/api/scenarios/${scenario.id}`, { method: "DELETE" });
      if (!response.ok && response.status !== 404) {
        const body = await response.json().catch(() => undefined) as { detail?: string } | undefined;
        throw new Error(typeof body?.detail === "string" ? body.detail : `Could not delete scenario (${response.status}).`);
      }
      setOpen(false);
      onDeleted(scenario.id);
      window.dispatchEvent(new Event("syda:scenarios-changed"));
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "Could not delete scenario. Please try again.");
    } finally {
      setDeleting(false);
    }
  };

  return <Dialog open={open} onOpenChange={(nextOpen) => { if (!deleting) { setOpen(nextOpen); setError(undefined); } }}>
    <DialogTrigger render={<Button variant="destructive" size="sm" aria-label={`Delete ${scenario.title}`} />}>Delete</DialogTrigger>
    <DialogContent showCloseButton={!deleting}>
      <DialogHeader>
        <DialogTitle>Delete scenario?</DialogTitle>
        <DialogDescription>Delete “{scenario.title}” from your saved scenarios? This cannot be undone. The chat and generated datasets will remain available. To remove the chat, use its Delete action under Recent chats.</DialogDescription>
      </DialogHeader>
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      <DialogFooter>
        <Button variant="outline" disabled={deleting} onClick={() => setOpen(false)}>Cancel</Button>
        <Button variant="destructive" disabled={deleting} onClick={deleteScenario}>
          {deleting && <Spinner data-icon="inline-start" />}
          {deleting ? "Deleting…" : "Delete scenario"}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

export default function Scenarios() {
  const [scenarios, setScenarios] = useState<SavedScenario[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/scenarios?limit=100", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Could not load scenarios (${response.status}).`);
        return response.json();
      })
      .then(setScenarios)
      .catch((fetchError) => { if (!controller.signal.aborted) setError(fetchError instanceof Error ? fetchError.message : "Scenarios are unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  return <AppShell title="Scenarios" subtitle="Saved definitions and drafts">
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
      <div className="mx-auto max-w-6xl space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h1 className="text-2xl font-semibold tracking-tight">Scenarios</h1><p className="mt-1 text-sm text-muted-foreground">Reuse and refine the workflows you have saved.</p></div>
          <Link className={buttonVariants({})} to="/app">New scenario</Link>
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {loading ? <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground"><Spinner /> Loading scenarios…</div> : scenarios.length === 0 ? (
          <Card><CardContent><Empty><EmptyHeader><EmptyTitle>No saved scenarios yet</EmptyTitle><EmptyDescription>Describe a scenario, then open it in the editor to save it here.</EmptyDescription></EmptyHeader><Button onClick={() => { window.location.href = "/app"; }}>Create a scenario</Button></Empty></CardContent></Card>
        ) : <Card>
          <CardHeader><CardTitle>Saved scenarios</CardTitle><CardDescription>{scenarios.length} most recent</CardDescription></CardHeader>
          <CardContent><Table>
            <TableHeader><TableRow><TableHead>Name</TableHead><TableHead className="hidden sm:table-cell">Instances</TableHead><TableHead className="hidden md:table-cell">Updated</TableHead><TableHead /></TableRow></TableHeader>
            <TableBody>{scenarios.map((scenario) => <TableRow key={scenario.id}>
              <TableCell className="min-w-0 whitespace-normal"><Link className="font-medium hover:underline" to={`/scenarios/${scenario.id}`}>{scenario.title}</Link><p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{scenario.description}</p></TableCell>
              <TableCell className="hidden tabular-nums sm:table-cell">{scenario.recordCount.toLocaleString()}</TableCell>
              <TableCell className="hidden text-muted-foreground md:table-cell">{new Date(scenario.updatedAt).toLocaleString()}</TableCell>
              <TableCell><div className="flex justify-end gap-2"><Link className={buttonVariants({ variant: "outline", size: "sm" })} to={`/scenarios/${scenario.id}`}>Open</Link><DeleteScenarioButton scenario={scenario} onDeleted={(id) => setScenarios((current) => current.filter((item) => item.id !== id))} /></div></TableCell>
            </TableRow>)}</TableBody>
          </Table></CardContent>
        </Card>}
      </div>
    </main>
  </AppShell>;
}
