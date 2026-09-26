import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { HugeiconsIcon } from "@hugeicons/react";
import { Database02Icon, Download01Icon } from "@hugeicons/core-free-icons";

import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "~/components/ui/empty";
import { Skeleton } from "~/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "~/components/ui/tabs";
import type { DatasetPreview } from "~/lib/studio-types";

const PAGE_SIZE = 25;
const COMPACT_PAGE_SIZE = 5;

export function DataPreview({ jobId, complete, showDownloads = true, compact = false }: { jobId?: string; complete: boolean; showDownloads?: boolean; compact?: boolean }) {
  const [offset, setOffset] = useState(0);
  const [selectedTable, setSelectedTable] = useState<string>();
  const pageSize = compact ? COMPACT_PAGE_SIZE : PAGE_SIZE;
  const previewQuery = useQuery<DatasetPreview, Error>({
    queryKey: ["generation-preview", jobId, pageSize, offset],
    enabled: complete && Boolean(jobId),
    staleTime: 60_000,
    queryFn: async ({ signal }) => {
      if (!jobId) throw new Error("Run ID is missing.");
      const response = await fetch(`/api/preview/${jobId}?limit=${pageSize}&offset=${offset}`, { signal });
      if (!response.ok) throw new Error(`Preview failed (${response.status})`);
      return response.json() as Promise<DatasetPreview>;
    },
  });

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

  if (previewQuery.isPending) {
    if (compact) return <section aria-label="Dataset preview" className="flex flex-col gap-3 rounded-2xl border bg-muted/20 p-4"><Skeleton className="h-5 w-40" /><Skeleton className="h-28 w-full" /></section>;
    return <Card><CardContent className="flex flex-col gap-3"><Skeleton className="h-8 w-56" /><Skeleton className="h-64 w-full" /></CardContent></Card>;
  }

  const preview = previewQuery.data;
  if (previewQuery.isError || !preview) {
    if (compact) return <p className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive" role="alert">Dataset preview is unavailable. Open the run to retry.</p>;
    return (
      <Card><CardContent><Empty><EmptyHeader><EmptyTitle>Preview unavailable</EmptyTitle><EmptyDescription>{previewQuery.error?.message ?? "The dataset could not be loaded."}</EmptyDescription></EmptyHeader></Empty></CardContent></Card>
    );
  }

  const tableNames = Object.keys(preview.tables);
  if (tableNames.length === 0) {
    return <Card><CardContent><Empty><EmptyHeader><EmptyTitle>No tables found</EmptyTitle><EmptyDescription>This run has no readable table files.</EmptyDescription></EmptyHeader></Empty></CardContent></Card>;
  }
  const activeTableName = selectedTable && preview.tables[selectedTable] ? selectedTable : tableNames[0];
  const activeTable = preview.tables[activeTableName];
  const rowStart = activeTable.rows.length ? offset + 1 : 0;
  const rowEnd = offset + activeTable.rows.length;
  const tableContent = (
    <Tabs
      value={activeTableName}
      onValueChange={(value) => {
        setSelectedTable(String(value));
        setOffset(0);
      }}
    >
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
              <TableHeader><TableRow>{table.columns.map((column) => <TableHead className={compact ? "h-9 px-2 text-xs" : undefined} key={column}>{column}</TableHead>)}</TableRow></TableHeader>
              <TableBody>
                {table.rows.slice(0, pageSize).map((row, rowIndex) => (
                  <TableRow key={rowIndex}>
                    {table.columns.map((column) => <TableCell className={compact ? "p-2 text-xs" : undefined} key={column}>{String(row[column] ?? "—")}</TableCell>)}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {!compact && <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-3">
              <p className="text-xs text-muted-foreground">
                Rows {rowStart.toLocaleString()}–{rowEnd.toLocaleString()} of {table.rowCount.toLocaleString()}
              </p>
              <div className="flex items-center gap-2">
                <Button disabled={offset === 0 || previewQuery.isFetching} onClick={() => setOffset(Math.max(0, offset - pageSize))} size="sm" variant="outline">
                  Previous
                </Button>
                <span className="text-xs text-muted-foreground">
                  Page {Math.floor(offset / pageSize) + 1} of {Math.max(1, Math.ceil(table.rowCount / pageSize))}
                </span>
                <Button disabled={rowEnd >= table.rowCount || previewQuery.isFetching} onClick={() => setOffset(offset + pageSize)} size="sm" variant="outline">
                  Next
                </Button>
              </div>
            </div>}
          </TabsContent>
        );
      })}
    </Tabs>
  );

  if (compact) {
    return (
      <section aria-label="Dataset preview" className="min-w-0 rounded-2xl border bg-muted/20 p-4">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h3 className="text-sm font-medium">Dataset preview</h3>
          <p className="text-xs text-muted-foreground">First {COMPACT_PAGE_SIZE} rows per table</p>
        </div>
        {tableContent}
      </section>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Generated dataset</CardTitle>
      </CardHeader>
      <CardContent>
        {tableContent}
      </CardContent>
      {showDownloads && (
        <CardFooter className="justify-end gap-2">
          <Button onClick={() => window.open(`/api/download/${jobId}?format=json`, "_blank")} type="button" variant="outline">
            <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Download JSON
          </Button>
          <Button onClick={() => window.open(`/api/download/${jobId}?format=csv`, "_blank")} type="button">
            <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Download CSV
          </Button>
        </CardFooter>
      )}
    </Card>
  );
}
