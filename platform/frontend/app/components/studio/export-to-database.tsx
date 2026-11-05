import { useState } from "react";
import { Link } from "react-router";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "~/components/ui/dialog";
import { useLiveProfiles } from "./live-connections";

type ExportResult = { tablesExported: string[]; totalRows: number };

export function ExportToDatabase({ jobId }: { jobId: string }) {
  const { profiles } = useLiveProfiles();
  const [open, setOpen] = useState(false);
  const [profileId, setProfileId] = useState("");
  const [password, setPassword] = useState("");
  const [ifExists, setIfExists] = useState<"append" | "replace" | "fail">("append");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ExportResult>();
  const profile = profiles.find(p => p.id === profileId) ?? profiles[0];

  const close = (next: boolean) => { setOpen(next); if (!next) { setPassword(""); setError(""); setResult(undefined); } };
  const exportData = async () => {
    if (!profile) return;
    setBusy(true); setError(""); setResult(undefined);
    try {
      const response = await fetch("/api/connections/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...profile, port: Number(profile.port), password, jobId, ifExists }), signal: AbortSignal.timeout(300000) });
      const body = await response.json().catch(() => ({ detail: "The export service is unavailable. Check that the backend is running, then retry." }));
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `Export failed (${response.status}).`);
      setResult(body);
    } catch (cause) {
      setError(cause instanceof Error && cause.name === "TimeoutError" ? "The export timed out. Some tables may have been written; check the database before retrying." : cause instanceof Error ? cause.message : "Export failed. Please retry.");
    } finally { setBusy(false); }
  };

  return <>
    <Button size="sm" variant="outline" onClick={() => setOpen(true)}>Export to database</Button>
    <Dialog open={open} onOpenChange={close}><DialogContent className="w-[calc(100%-2rem)] max-w-lg">
      <DialogHeader><DialogTitle>Export to database</DialogTitle><DialogDescription>Write every generated table into a saved connection, parents before children.</DialogDescription></DialogHeader>
      {!profiles.length ? <p className="text-sm text-muted-foreground">No saved connections yet. <Link className="underline" to="/connections">Add one on the Connections page</Link>, then come back.</p> : <div className="grid gap-3">
        <label className="flex flex-col gap-1 text-xs">Connection<select className="h-9 rounded-xl border bg-background px-3" disabled={busy} value={profile?.id} onChange={e => setProfileId(e.target.value)}>{profiles.map(p => <option key={p.id} value={p.id}>{p.name} — {p.dialect} · {p.host}/{p.database}</option>)}</select></label>
        <label className="flex flex-col gap-1 text-xs">Password for {profile?.username}<Input type="password" autoComplete="off" disabled={busy} value={password} onChange={e => setPassword(e.target.value)} /></label>
        <label className="flex flex-col gap-1 text-xs">If a table already exists<select className="h-9 rounded-xl border bg-background px-3" disabled={busy} value={ifExists} onChange={e => setIfExists(e.target.value as typeof ifExists)}><option value="append">Append rows</option><option value="replace">Replace table (drops existing data)</option><option value="fail">Stop without writing</option></select></label>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {result && <p role="status" className="text-sm">Exported {result.totalRows.toLocaleString()} rows to {result.tablesExported.join(", ")}.</p>}
        <Button disabled={busy || !password} onClick={() => void exportData()}>{busy ? "Exporting…" : "Export"}</Button>
      </div>}
    </DialogContent></Dialog>
  </>;
}
