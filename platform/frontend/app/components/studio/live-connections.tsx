import { useEffect, useRef, useState } from "react";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "~/components/ui/dialog";
import type { SchemaTable } from "~/lib/studio-types";

export type Profile = { id: string; name: string; dialect: "PostgreSQL" | "MySQL" | "Oracle"; host: string; port: string; database: string; username: string };
export type SchemaSource = { name: string; schemas: Record<string, SchemaTable> };
export const newProfile = (): Profile => ({ id: crypto.randomUUID(), name: "", dialect: "PostgreSQL", host: "", port: "5432", database: "", username: "" });
const defaultPort = { PostgreSQL: "5432", MySQL: "3306", Oracle: "1521" } as const;

// Saved profiles only: an entry appears after GET /api/connections/profiles or a successful save().
export function useLiveProfiles() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  useEffect(() => {
    void fetch("/api/connections/profiles").then(r => r.ok ? r.json() : []).then((saved: (Omit<Profile, "port"> & { port: number })[]) => {
      // Keep any profile saved before the list arrived.
      setProfiles(prev => [...saved.map(p => ({ ...p, port: String(p.port) })), ...prev.filter(p => !saved.some(s => s.id === p.id))]);
    }).catch(() => undefined);
  }, []);
  const save = async (profile: Profile) => {
    const response = await fetch(`/api/connections/profiles/${profile.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...profile, port: Number(profile.port) }) });
    if (!response.ok) throw new Error("Schema loaded, but the connection profile could not be saved. Check the connection name and retry.");
    setProfiles(prev => prev.some(p => p.id === profile.id) ? prev.map(p => p.id === profile.id ? profile : p) : [...prev, profile]);
  };
  const remove = (id: string) => { setProfiles(prev => prev.filter(p => p.id !== id)); void fetch(`/api/connections/profiles/${id}`, { method: "DELETE" }).catch(() => undefined); };
  return { profiles, save, remove };
}

const fields = [["name", "Connection name"], ["host", "Host"], ["port", "Port"], ["database", "Database / service name"], ["username", "Username"]] as const;

/** Add or edit one profile, then test it and load its table structure. Mount with a `key` per opening so state starts fresh. */
export function ConnectionDialog({ initial, isSaved, open, onOpenChange, onLoaded, save }: {
  initial: Profile; isSaved: boolean; open: boolean; onOpenChange: (open: boolean) => void;
  onLoaded: (source: SchemaSource) => void; save: (profile: Profile) => Promise<void>;
}) {
  const [profile, setProfile] = useState(initial);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const version = useRef(0);
  const update = (patch: Partial<Profile>) => { version.current++; setError(""); setProfile(p => ({ ...p, ...patch })); };
  const close = (next: boolean) => { if (!next) version.current++; onOpenChange(next); };
  const port = Number(profile.port);
  const ready = profile.host.trim() && profile.database.trim() && profile.username.trim() && /^\d+$/.test(profile.port) && port >= 1 && port <= 65535;

  const inspect = async () => {
    const requestVersion = ++version.current;
    const named = { ...profile, name: (profile.name.trim() || `${profile.database} on ${profile.host}`).slice(0, 120) };
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/connections/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...named, port, password }), signal: AbortSignal.timeout(60000) });
      const result = await response.json().catch(() => ({ detail: "The database service is unavailable. Check that the backend is running, then retry." }));
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : response.status === 401 ? "Sign in again to connect to a database." : "Check the connection fields and try again.");
      if (requestVersion !== version.current) return;
      onLoaded({ name: named.name, schemas: result.schemas });
      await save(named);
      if (requestVersion === version.current) close(false);
    } catch (cause) {
      if (requestVersion === version.current) setError(cause instanceof Error && cause.name === "TimeoutError" ? "The connection timed out. Check server access to this database and retry." : cause instanceof Error ? cause.message : "Connection failed. Please retry.");
    } finally { if (requestVersion === version.current) setBusy(false); }
  };

  return <Dialog open={open} onOpenChange={close}>
    <DialogContent className="w-[calc(100%-2rem)] max-w-xl max-h-[85svh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{isSaved ? initial.name : "Add connection"}</DialogTitle>
        <DialogDescription>Syda reads table structure only, never records, and uses it to build a scenario. The profile is saved after a successful test; the password never is.</DialogDescription>
      </DialogHeader>
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={e => { e.preventDefault(); if (ready && !busy) void inspect(); }}>
        <label className="flex flex-col gap-1 text-xs">Database type
          <select className="h-9 rounded-xl border bg-background px-3" disabled={busy} value={profile.dialect} onChange={e => { const dialect = e.target.value as Profile["dialect"]; update({ dialect, port: defaultPort[dialect] }); }}>
            {Object.keys(defaultPort).map(d => <option key={d}>{d}</option>)}
          </select>
        </label>
        {fields.map(([key, label]) => <label key={key} className="flex flex-col gap-1 text-xs">{label}
          <Input disabled={busy} value={profile[key]} placeholder={key === "name" ? "Optional" : undefined} onChange={e => update({ [key]: e.target.value })} />
          {key === "host" && <span className="text-muted-foreground">Must be reachable from the Syda server</span>}
        </label>)}
        <label className="flex flex-col gap-1 text-xs">Password
          <Input type="password" autoComplete="off" autoFocus={isSaved} disabled={busy} value={password} onChange={e => { setPassword(e.target.value); setError(""); }} />
        </label>
        {error && <p role="alert" className="text-sm text-destructive sm:col-span-2">{error}</p>}
        <DialogFooter className="sm:col-span-2">
          <Button type="button" variant="outline" onClick={() => close(false)}>Cancel</Button>
          <Button type="submit" disabled={busy || !ready}>{busy ? "Reading schema…" : error ? "Retry" : "Test & load schema"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}
