import { useEffect, useRef, useState } from "react";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "~/components/ui/dialog";
import { SchemaExplorer } from "./schema-explorer";
import type { SchemaTable } from "~/lib/studio-types";

export type Profile = { id: string; name: string; dialect: "PostgreSQL" | "MySQL" | "Oracle"; host: string; port: string; database: string; username: string };
export type SchemaSource = { name: string; schemas: Record<string, SchemaTable> };
const fresh = (): Profile => ({ id: crypto.randomUUID(), name: "New connection", dialect: "PostgreSQL", host: "", port: "5432", database: "", username: "" });

export function useLiveProfiles() {
  // Saved profiles only. The dialog edits `draft`, a copy that joins `profiles` once save() succeeds.
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [draft, setDraft] = useState<Profile | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    void fetch("/api/connections/profiles").then(r => r.ok ? r.json() : []).then((saved: (Omit<Profile, "port"> & { port: number })[]) => {
      // Keep any profile added before the list arrived.
      setProfiles(prev => [...saved.map(p => ({ ...p, port: String(p.port) })), ...prev.filter(p => !saved.some(s => s.id === p.id))]);
    }).catch(() => undefined);
  }, []);
  const save = async (profile: Profile) => {
    const response = await fetch(`/api/connections/profiles/${profile.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...profile, port: Number(profile.port) }) });
    if (!response.ok) throw new Error("Schema loaded, but the connection profile could not be saved. Check the connection name and retry.");
    setProfiles(prev => prev.some(p => p.id === profile.id) ? prev.map(p => p.id === profile.id ? profile : p) : [...prev, profile]);
  };
  const remove = (id: string) => { setProfiles(prev => prev.filter(p => p.id !== id)); void fetch(`/api/connections/profiles/${id}`, { method: "DELETE" }).catch(() => undefined); };
  // Opens the dialog on the given profile, else the first saved one, else a new draft. Any earlier unsaved draft is dropped.
  const show = (id?: string) => { const p = profiles.find(x => x.id === id) ?? profiles[0]; setDraft(p ? { ...p } : fresh()); setOpen(true); };
  return { profiles, draft, setDraft, open, setOpen, show, save, remove };
}
export function LiveConnections({ source, onSource, disabled, connections }: { source: SchemaSource | null; onSource: (value: SchemaSource | null) => void; disabled: boolean; connections: ReturnType<typeof useLiveProfiles> }) {
  const { profiles, draft: profile, setDraft, open, setOpen, show, save, remove } = connections;
  const [password, setPassword] = useState("");
  const [schemas, setSchemas] = useState<Record<string, SchemaTable> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const version = useRef(0);
  const active = profile?.id ?? "";
  const saved = profiles.some(p => p.id === active);
  const options = profile ? [...profiles.map(p => p.id === active ? profile : p), ...(saved ? [] : [profile])] : profiles;
  const reset = () => { version.current++; setBusy(false); setSchemas(null); setPassword(""); setError(""); };
  const add = () => { reset(); setDraft(fresh()); };
  const update = (patch: Partial<Profile>) => { version.current++; setSchemas(null); setError(""); setDraft(d => d && { ...d, ...patch }); };
  const inspect = async () => {
    if (!profile) return;
    const requestVersion = ++version.current;
    setBusy(true); setError(""); setSchemas(null);
    try {
      const response = await fetch("/api/connections/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...profile, port: Number(profile.port), password }), signal: AbortSignal.timeout(60000) });
      const result = await response.json().catch(() => ({ detail: "The database service is unavailable. Check that the backend is running, then retry." }));
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : response.status === 401 ? "Sign in again to connect to a database." : "Check the connection fields and try again.");
      if (requestVersion !== version.current) return;
      setSchemas(result.schemas);
      await save(profile);
    } catch (cause) {
      if (requestVersion === version.current) setError(cause instanceof Error && cause.name === "TimeoutError" ? "The connection timed out. Check server access to this database and retry." : cause instanceof Error ? cause.message : "Connection failed. Please retry.");
    } finally { if (requestVersion === version.current) setBusy(false); }
  };
  return <>
    <Button type="button" size="sm" variant="outline" className="h-9 min-w-0 max-w-full" disabled={disabled} onClick={() => show()}><span className="truncate">{source ? `Source: ${source.name}` : "Connect database"}</span></Button>
    {source && <Button type="button" size="sm" className="h-9" variant="ghost" disabled={disabled} onClick={() => onSource(null)}>Use CSV only</Button>}
    <Dialog open={open} onOpenChange={next => { setOpen(next); if (!next) reset(); }}><DialogContent className="w-[calc(100%-2rem)] max-w-3xl max-h-[85svh] overflow-y-auto"><DialogHeader><DialogTitle>Database connections</DialogTitle><DialogDescription>Syda reads table structure only, never records, and uses it to build a scenario. Profiles are saved to your account after a successful test; passwords are never saved.</DialogDescription></DialogHeader>
      <div className="flex flex-wrap items-end gap-2"><label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">Connection<select aria-label="Connection" className="h-9 w-full min-w-0 rounded-xl border bg-background px-3" value={active} disabled={busy} onChange={e => { reset(); show(e.target.value); }}>{options.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label><Button type="button" variant="outline" disabled={busy} onClick={add}>Add connection</Button></div>
      {profile && <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-xs">Database type<select className="h-9 rounded-xl border bg-background px-3" disabled={busy} value={profile.dialect} onChange={e => { const dialect = e.target.value as Profile['dialect']; update({ dialect, port: dialect === "MySQL" ? "3306" : dialect === "Oracle" ? "1521" : "5432" }); }}><option>PostgreSQL</option><option>MySQL</option><option>Oracle</option></select></label>
        {([['name', 'Connection name'], ['host', 'Host'], ['port', 'Port'], ['database', 'Database / service name'], ['username', 'Username']] as const).map(([key, label]) => <label key={key} className="flex flex-col gap-1 text-xs">{label}<Input disabled={busy} value={profile[key]} onChange={e => update({ [key]: e.target.value })} />{key === "host" && <span className="text-muted-foreground">Must be reachable from the Syda server</span>}</label>)}
        <label className="flex flex-col gap-1 text-xs">Password<Input type="password" autoComplete="off" disabled={busy} value={password} onChange={e => { setPassword(e.target.value); setSchemas(null); }} /></label>
      </div>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button type="button" disabled={busy || !profile?.database.trim() || !profile?.username.trim() || !profile?.host.trim() || !/^\d+$/.test(profile?.port ?? "") || Number(profile?.port) < 1 || Number(profile?.port) > 65535} onClick={() => void inspect()}>{busy ? "Reading schema…" : error ? "Retry connection" : "Test & load schema"}</Button>{profile && <Button type="button" variant="outline" disabled={busy} onClick={() => { reset(); if (saved) remove(active); const next = profiles.find(x => x.id !== active); if (next) setDraft({ ...next }); else setOpen(false); }}>{saved ? "Remove profile" : "Discard"}</Button>}</div>
      {schemas && <><SchemaExplorer key={active} schemas={schemas} /><p className="text-xs text-muted-foreground">The imported structure is used to build your scenario and is shared with the configured AI provider. Review the generated plan before running it.</p><Button type="button" disabled={!Object.keys(schemas).length} onClick={() => { onSource({ name: profile?.name || "Database", schemas }); setOpen(false); reset(); }}>Use this schema</Button></>}
    </DialogContent></Dialog>
  </>;
}
