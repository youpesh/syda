import { useState } from "react";
import { useNavigate } from "react-router";

import type { Route } from "./+types/connections";
import { AppShell } from "~/components/studio/app-shell";
import { ConnectionDialog, DdlDialog, newProfile, useLiveProfiles, type Profile, type SchemaSource } from "~/components/studio/live-connections";
import { SchemaExplorer } from "~/components/studio/schema-explorer";
import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "~/components/ui/empty";

export function meta({}: Route.MetaArgs) {
  return [{ title: "Connections — Syda" }];
}

export default function Connections() {
  const navigate = useNavigate();
  const { profiles, save, remove } = useLiveProfiles();
  const [source, setSource] = useState<SchemaSource | null>(null);
  // `editing` outlives `open` so the dialog keeps its content while it animates closed.
  const [editing, setEditing] = useState<{ profile: Profile; saved: boolean; key: number } | null>(null);
  const [open, setOpen] = useState(false);
  const [pasting, setPasting] = useState(false);
  const edit = (profile: Profile) => { setEditing({ profile: { ...profile }, saved: profiles.some(p => p.id === profile.id), key: Date.now() }); setOpen(true); };
  const tables = source ? Object.keys(source.schemas).length : 0;

  return <AppShell title="Connections" subtitle="Database schemas for your scenarios">
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
      <div className="mx-auto flex max-w-5xl flex-col gap-5">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Connections</h1>
          <p className="mt-1 text-sm text-muted-foreground">Connect a database, or paste its DDL, to build scenarios from its table structure.</p>
        </div>
        <Card>
          <CardHeader>
            <CardTitle>Database connections</CardTitle>
            <CardDescription>PostgreSQL, MySQL, Oracle, or SQL Server. Syda reads table structure only; it never reads or writes records. Passwords are never saved. If the Syda server cannot reach your database (private network, localhost), paste its DDL instead.</CardDescription>
            {profiles.length > 0 && <CardAction className="flex gap-2"><Button size="sm" variant="outline" onClick={() => setPasting(true)}>Paste DDL</Button><Button size="sm" onClick={() => edit(newProfile())}>Add connection</Button></CardAction>}
          </CardHeader>
          <CardContent>
            {profiles.length === 0
              ? <Empty><EmptyHeader><EmptyTitle>No connections yet</EmptyTitle><EmptyDescription>Add a database to import its table structure into a scenario, or paste its CREATE TABLE script.</EmptyDescription></EmptyHeader><div className="flex flex-wrap justify-center gap-2"><Button onClick={() => edit(newProfile())}>Add connection</Button><Button variant="outline" onClick={() => setPasting(true)}>Paste DDL</Button></div></Empty>
              : <ul className="divide-y rounded-xl border text-sm">{profiles.map(p => <li key={p.id} className="flex flex-col gap-2 px-3 py-2 sm:flex-row sm:items-center sm:gap-3">
                  <div className="min-w-0 sm:flex-1">
                    <div className="flex items-center gap-2"><span className="truncate font-medium">{p.name}</span><Badge variant="outline" className="shrink-0">{p.dialect}</Badge></div>
                    <p className="truncate text-xs text-muted-foreground">{p.username}@{p.host}:{p.port}/{p.database}</p>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    <Button size="sm" variant="outline" onClick={() => edit(p)}>Load schema</Button>
                    <Button size="sm" variant="ghost" onClick={() => remove(p.id)}>Remove</Button>
                  </div>
                </li>)}</ul>}
          </CardContent>
        </Card>
        {source && <Card>
          <CardHeader>
            <CardTitle>{source.name}</CardTitle>
            <CardDescription>{tables ? `${tables} tables ready to use in a new scenario. The structure is shared with the configured AI provider.` : "No tables found in this database."}</CardDescription>
          </CardHeader>
          <CardContent><SchemaExplorer key={source.name} schemas={source.schemas} /></CardContent>
          <CardFooter className="gap-2">
            <Button disabled={!tables} onClick={() => navigate("/app", { state: { schemaSource: source } })}>Use schema in new chat</Button>
            <Button variant="ghost" onClick={() => setSource(null)}>Clear</Button>
          </CardFooter>
        </Card>}
      </div>
    </main>
    <DdlDialog open={pasting} onOpenChange={setPasting} onLoaded={setSource} />
    {editing && <ConnectionDialog key={editing.key} initial={editing.profile} isSaved={editing.saved} open={open} onOpenChange={setOpen} onLoaded={setSource} save={save} />}
  </AppShell>;
}
