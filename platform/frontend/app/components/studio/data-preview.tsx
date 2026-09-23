import { useEffect, useState } from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import { Database02Icon, Download01Icon } from "@hugeicons/core-free-icons";

import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "~/components/ui/empty";
import { Skeleton } from "~/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "~/components/ui/tabs";
import type { DatasetPreview } from "~/lib/studio-types";

export function DataPreview({ jobId, complete }: { jobId?: string; complete: boolean }) {
  const [preview, setPreview] = useState<DatasetPreview>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!complete || !jobId) return;
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    fetch(`/api/preview/${jobId}?limit=10`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Preview failed (${response.status})`);
        return response.json();
      })
      .then(setPreview)
      .catch((fetchError) => {
        if (!controller.signal.aborted) setError(fetchError instanceof Error ? fetchError.message : "Preview unavailable");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [complete, jobId]);

  if (!complete || !jobId) {
    return (
      <Card>
        <CardContent>
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon"><HugeiconsIcon icon={Database02Icon} strokeWidth={2} /></EmptyMedia>
              <EmptyTitle>No generated dataset yet</EmptyTitle>
              <EmptyDescription>Validate the configuration and run generation to preview each output table.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        </CardContent>
      </Card>
    );
  }

  if (loading) {
    return <Card><CardContent className="flex flex-col gap-3"><Skeleton className="h-8 w-56" /><Skeleton className="h-64 w-full" /></CardContent></Card>;
  }

  if (error || !preview) {
    return (
      <Card><CardContent><Empty><EmptyHeader><EmptyTitle>Preview unavailable</EmptyTitle><EmptyDescription>{error ?? "The dataset could not be loaded."}</EmptyDescription></EmptyHeader></Empty></CardContent></Card>
    );
  }

  const tableNames = Object.keys(preview.tables);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Generated dataset</CardTitle>
        <CardDescription>Showing the first 10 records from each table.</CardDescription>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue={tableNames[0]}>
          <TabsList variant="line" className="max-w-full overflow-x-auto">
            {tableNames.map((name) => (
              <TabsTrigger key={name} value={name}>{name}<Badge variant="outline">{preview.tables[name].rowCount.toLocaleString()}</Badge></TabsTrigger>
            ))}
          </TabsList>
          {tableNames.map((name) => {
            const table = preview.tables[name];
            return (
              <TabsContent className="pt-3" key={name} value={name}>
                <Table>
                  <TableHeader><TableRow>{table.columns.map((column) => <TableHead key={column}>{column}</TableHead>)}</TableRow></TableHeader>
                  <TableBody>
                    {table.rows.map((row, rowIndex) => (
                      <TableRow key={rowIndex}>
                        {table.columns.map((column) => <TableCell key={column}>{String(row[column] ?? "—")}</TableCell>)}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TabsContent>
            );
          })}
        </Tabs>
      </CardContent>
      <CardFooter className="justify-end gap-2">
        <Button onClick={() => window.open(`/api/download/${jobId}?format=json`, "_blank")} type="button" variant="outline">
          <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Download JSON
        </Button>
        <Button onClick={() => window.open(`/api/download/${jobId}?format=csv`, "_blank")} type="button">
          <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Download CSV
        </Button>
      </CardFooter>
    </Card>
  );
}
