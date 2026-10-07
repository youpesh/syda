import { useState } from "react";
import { useNavigate } from "react-router";

import type { Route } from "./+types/connections";
import { AppShell } from "~/components/studio/app-shell";
import { LiveConnections, useLiveProfiles, type SchemaSource } from "~/components/studio/live-connections";
import { SchemaExplorer } from "~/components/studio/schema-explorer";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "~/components/ui/card";

export function meta({}: Route.MetaArgs) {
  return [{ title: "Connections — Syda" }];
}

export default function Connections() {
  const navigate = useNavigate();
  const connections = useLiveProfiles();
  const [source, setSource] = useState<SchemaSource | null>(null);

  return <AppShell title="Connections" subtitle="Database schemas for your scenarios">
    <main className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
      <div className="mx-auto flex max-w-5xl flex-col gap-5">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Connections</h1>
          <p className="mt-1 text-sm text-muted-foreground">Connect a database to build scenarios from its table structure.</p>
        </div>
        <Card>
          <CardHeader>
            <CardTitle>Database connections</CardTitle>
            <CardDescription>Connect PostgreSQL, MySQL, or Oracle and review the imported schema. Syda reads table structure only; it does not read or write database records.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-wrap gap-2">
              <LiveConnections connections={connections} source={source} onSource={setSource} disabled={false} />
            </div>
            <p className="text-xs text-muted-foreground">Connection profiles last while this page is open. Passwords are never saved.</p>
          </CardContent>
        </Card>
        {source && <Card>
          <CardHeader>
            <CardTitle>{source.name}</CardTitle>
            <CardDescription>{Object.keys(source.schemas).length} tables ready to use in a new scenario.</CardDescription>
          </CardHeader>
          <CardContent><SchemaExplorer schemas={source.schemas} /></CardContent>
          <CardFooter>
            <Button onClick={() => navigate("/app", { state: { schemaSource: source } })}>Use schema in new chat</Button>
          </CardFooter>
        </Card>}
      </div>
    </main>
  </AppShell>;
}
