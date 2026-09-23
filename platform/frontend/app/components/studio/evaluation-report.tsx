import { HugeiconsIcon } from "@hugeicons/react";
import {
  AlertCircleIcon,
  CheckmarkCircle02Icon,
  Download01Icon,
  SecurityCheckIcon,
} from "@hugeicons/core-free-icons";

import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "~/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import type { JobStats } from "~/lib/studio-types";

const statusVariant = {
  pass: "secondary",
  fail: "destructive",
  not_evaluated: "outline",
} as const;

export function EvaluationReport({ stats, jobId }: { stats?: JobStats; jobId?: string }) {
  if (!stats || !jobId) {
    return (
      <Card>
        <CardContent>
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon"><HugeiconsIcon icon={SecurityCheckIcon} strokeWidth={2} /></EmptyMedia>
              <EmptyTitle>No evaluation report yet</EmptyTitle>
              <EmptyDescription>Evaluation runs automatically after generation and produces a quality-gate report.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle>Evaluation report</CardTitle>
            <CardDescription>{stats.recordsGenerated.toLocaleString()} records evaluated in {stats.durationSeconds}s.</CardDescription>
          </div>
          <Badge variant={stats.evaluationResult === "fail" ? "destructive" : stats.evaluationResult === "pass" ? "secondary" : "outline"}>
            <HugeiconsIcon data-icon="inline-start" icon={stats.evaluationResult === "fail" ? AlertCircleIcon : CheckmarkCircle02Icon} strokeWidth={2} />
            {stats.evaluationResult === "partial" ? "Partial evaluation" : stats.evaluationResult}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!!Object.keys(stats.pathCounts).length && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium">Scenario paths</span>
            {Object.entries(stats.pathCounts).map(([path, count]) => (
              <Badge key={path} variant="outline">{path} · {count.toLocaleString()}</Badge>
            ))}
          </div>
        )}
        <dl className="grid gap-3 sm:grid-cols-3">
          <div className="border-l pl-3"><dt className="text-xs text-muted-foreground">Causal consistency</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{stats.causalIntegrity}</dd></div>
          <div className="border-l pl-3"><dt className="text-xs text-muted-foreground">Referential integrity</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{stats.referentialIntegrity}</dd></div>
          <div className="border-l pl-3"><dt className="text-xs text-muted-foreground">Flagged checks</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{stats.flaggedRecords}</dd></div>
        </dl>
        <Table>
          <TableHeader>
            <TableRow><TableHead>Metric</TableHead><TableHead>Status</TableHead><TableHead>Value</TableHead><TableHead>Violations</TableHead><TableHead>Detail</TableHead></TableRow>
          </TableHeader>
          <TableBody>
            {stats.evaluationMetrics.map((metric) => (
              <TableRow key={metric.key}>
                <TableCell className="font-medium">{metric.label}</TableCell>
                <TableCell><Badge variant={statusVariant[metric.status]}>{metric.status.replace("_", " ")}</Badge></TableCell>
                <TableCell className="tabular-nums">{metric.value}</TableCell>
                <TableCell className="tabular-nums">{metric.violations}</TableCell>
                <TableCell className="max-w-xs whitespace-normal text-muted-foreground">{metric.detail}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
      <CardFooter className="justify-end gap-2">
        <Button onClick={() => window.open(`/api/evaluation/${jobId}/report?format=json`, "_blank")} type="button" variant="outline">
          <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Report JSON
        </Button>
        <Button onClick={() => window.open(`/api/evaluation/${jobId}/report?format=html`, "_blank")} type="button">
          <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Report HTML
        </Button>
      </CardFooter>
    </Card>
  );
}
