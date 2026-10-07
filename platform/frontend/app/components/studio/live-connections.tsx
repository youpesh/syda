import { useRef, useState } from "react";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "~/components/ui/dialog";
import { SchemaExplorer } from "./schema-explorer";
import type { SchemaTable } from "~/lib/studio-types";

type Profile = { id: string; name: string; dialect: "PostgreSQL" | "MySQL" | "Oracle"; host: string; port: string; database: string; username: string };
export type SchemaSource = { name: string; schemas: Record<string, SchemaTable> };
const fresh = (): Profile => ({ id: crypto.randomUUID(), name: "New connection", dialect: "PostgreSQL", host: "localhost", port: "5432", database: "", username: "" });

export function useLiveProfiles() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [active, setActive] = useState("");
  return { profiles, setProfiles, active, setActive };
}
export function LiveConnections({ source, onSource, disabled, connections }: { source: SchemaSource | null; onSource: (value: SchemaSource | null) => void; disabled: boolean; connections: ReturnType<typeof useLiveProfiles> }) {
  const [open, setOpen] = useState(false);
  const { profiles, setProfiles, active, setActive } = connections;
  const [password, setPassword] = useState("");
  const [schemas, setSchemas] = useState<Record<string, SchemaTable> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const version = useRef(0);
  const profile = profiles.find(p => p.id === active);
  const reset = () => { version.current++; setBusy(false); setSchemas(null); setPassword(""); setError(""); };
  const add = () => { reset(); const p = fresh(); setProfiles(prev => [...prev, p]); setActive(p.id); };
  const update = (patch: Partial<Profile>) => { version.current++; setSchemas(null); setError(""); setProfiles(prev => prev.map(p => p.id === active ? { ...p, ...patch } : p)); };
  const inspect = async () => {
    if (!profile) return;
    const requestVersion = ++version.current;
    setBusy(true); setError(""); setSchemas(null);
    try {
      const response = await fetch("/api/connections/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...profile, port: Number(profile.port), password }), signal: AbortSignal.timeout(60000) });
      const result = await response.json().catch(() => ({ detail: "The database service is unavailable. Check that the backend is running, then retry." }));
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : response.status === 401 ? "Sign in again to connect to a database." : "Check the connection fields and try again.");
      if (requestVersion === version.current) setSchemas(result.schemas);
    } catch (cause) {
      if (requestVersion === version.current) setError(cause instanceof Error && cause.name === "TimeoutError" ? "The connection timed out. Check server access to this database and retry." : cause instanceof Error ? cause.message : "Connection failed. Please retry.");
    } finally { if (requestVersion === version.current) setBusy(false); }
  };
  return <>
    <Button type="button" size="sm" variant="outline" className="h-9 min-w-0 max-w-full" disabled={disabled} onClick={() => { setOpen(true); if (!profiles.length) add(); }}><span className="truncate">{source ? `Source: ${source.name}` : "Connect database"}</span></Button>
    {source && <Button type="button" size="sm" className="h-9" variant="ghost" disabled={disabled} onClick={() => onSource(null)}>Use CSV only</Button>}
    <Dialog open={open} onOpenChange={next => { setOpen(next); if (!next) reset(); }}><DialogContent className="w-[calc(100%-2rem)] max-w-3xl max-h-[85svh] overflow-y-auto"><DialogHeader><DialogTitle>Database connections</DialogTitle><DialogDescription>Import table structure into your prompt. No database records are read or written. Profiles last while this chat page is open; passwords are never saved.</DialogDescription></DialogHeader>
      <div className="flex flex-wrap items-end gap-2"><label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">Connection<select aria-label="Connection" className="h-9 w-full min-w-0 rounded-xl border bg-background px-3" value={active} disabled={busy} onChange={e => { reset(); setActive(e.target.value); }}>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label><Button type="button" variant="outline" disabled={busy} onClick={add}>Add connection</Button></div>
      {profile && <div className="grid gap-3 sm:grid-cols-2">
        {([['name', 'Connection name'], ['host', 'Host'], ['port', 'Port'], ['database', 'Database / service name'], ['username', 'Username']] as const).map(([key, label]) => <label key={key} className="flex flex-col gap-1 text-xs">{label}<Input disabled={busy} value={profile[key]} onChange={e => update({ [key]: e.target.value })} /></label>)}
        <label className="flex flex-col gap-1 text-xs">Database type<select className="h-9 rounded-xl border bg-background px-3" disabled={busy} value={profile.dialect} onChange={e => { const dialect = e.target.value as Profile['dialect']; update({ dialect, port: dialect === "MySQL" ? "3306" : dialect === "Oracle" ? "1521" : "5432" }); }}><option>PostgreSQL</option><option>MySQL</option><option>Oracle</option></select></label>
        <label className="flex flex-col gap-1 text-xs">Password<Input type="password" autoComplete="off" disabled={busy} value={password} onChange={e => { setPassword(e.target.value); setSchemas(null); }} /></label>
      </div>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button type="button" disabled={busy || !profile?.database.trim() || !profile?.username.trim() || !profile?.host.trim() || !/^\d+$/.test(profile?.port ?? "") || Number(profile?.port) < 1 || Number(profile?.port) > 65535} onClick={() => void inspect()}>{busy ? "Reading schema…" : error ? "Retry connection" : "Test & load schema"}</Button>{profile && <Button type="button" variant="outline" disabled={busy} onClick={() => { reset(); setProfiles(p => p.filter(x => x.id !== active)); setActive(profiles.find(x => x.id !== active)?.id ?? ""); }}>Remove profile</Button>}</div>
      {schemas && <><SchemaExplorer key={active} schemas={schemas} /><p className="text-xs text-muted-foreground">The imported schema will be included with your next prompt and shared with the configured AI provider. Review the generated plan before running it. Output is a CSV download.</p><Button type="button" disabled={!Object.keys(schemas).length} onClick={() => { onSource({ name: profile?.name || "Database", schemas }); setOpen(false); reset(); }}>Use this schema</Button></>}
    </DialogContent></Dialog>
  </>;
}
