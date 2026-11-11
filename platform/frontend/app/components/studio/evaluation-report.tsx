import { HugeiconsIcon } from "@hugeicons/react";
import {
  AlertCircleIcon,
  ArrowUpRight01Icon,
  CheckmarkCircle02Icon,
  Download01Icon,
  InformationCircleIcon,
  MinusSignCircleIcon,
  SecurityCheckIcon,
} from "@hugeicons/core-free-icons";

import { Badge } from "~/components/ui/badge";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "~/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "~/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "~/components/ui/table";
import type { JobStats } from "~/lib/studio-types";
import { useTheme } from "~/lib/theme-provider";

type EvaluationMetric = JobStats["evaluationMetrics"][number];
type DisplayStatus = EvaluationMetric["status"] | "not_applicable";

const MAX_VISIBLE_JUDGE_ANOMALIES = 10;

const statusPresentation = {
  pass: {
    badgeClassName: "border-emerald-500/30 bg-emerald-500/10 text-emerald-500",
    icon: CheckmarkCircle02Icon,
    label: "pass",
  },
  fail: {
    badgeClassName: "",
    icon: AlertCircleIcon,
    label: "fail",
  },
  not_evaluated: {
    badgeClassName: "text-muted-foreground",
    icon: MinusSignCircleIcon,
    label: "not evaluated",
  },
  not_applicable: {
    badgeClassName: "text-muted-foreground",
    icon: InformationCircleIcon,
    label: "not applicable",
  },
} as const;

const judgePresentation = {
  pass: {
    badgeClassName: "border-emerald-500/30 bg-emerald-500/10 text-emerald-600",
    containerClassName: "border-emerald-500/30 bg-emerald-500/5",
    icon: CheckmarkCircle02Icon,
    label: "Pass",
  },
  warning: {
    badgeClassName: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    containerClassName: "border-amber-500/40 bg-amber-500/5",
    icon: InformationCircleIcon,
    label: "Warning",
  },
  fail: {
    badgeClassName: "",
    containerClassName: "border-destructive/40 bg-destructive/5",
    icon: AlertCircleIcon,
    label: "Fail",
  },
} as const;

function displayStatus(metric: EvaluationMetric): DisplayStatus {
  if (metric.key === "referential_integrity" && metric.value.toLowerCase() === "not applicable") {
    return "not_applicable";
  }
  return metric.status;
}

function privacySummary(metric: EvaluationMetric) {
  const value = /k=(\d+);\s*risk=([\d.]+)%/i.exec(metric.value);
  const evaluated = /Evaluated\s+([\d,]+)\s+records\s+using\s+(.+?)\.(?:\s|$)/i.exec(metric.detail);
  const threshold = /smaller than\s+(\d+)/i.exec(metric.detail);
  const source = evaluated?.[2];
  const sourceParts = source ? /^(.+?)\s+\((.+)\)$/.exec(source) : null;

  return {
    evaluatedRecords: evaluated?.[1],
    minimumK: value?.[1],
    risk: value?.[2],
    threshold: threshold?.[1],
    quasiIdentifiers: sourceParts
      ? `${sourceParts[1]}: ${sourceParts[2].split(",").map((item) => item.trim()).join(" + ")}`
      : source,
  };
}

export function EvaluationReport({ stats, jobId }: { stats?: JobStats; jobId?: string }) {
  const { resolvedTheme } = useTheme();

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

  const privacyMetric = stats.evaluationMetrics.find((metric) => metric.key === "privacy_leakage");
  const showPrivacyFailure = privacyMetric?.status === "fail";
  const privacy = privacyMetric ? privacySummary(privacyMetric) : undefined;
  const displayedMetrics = showPrivacyFailure
    ? stats.evaluationMetrics.filter((metric) => metric.key !== "privacy_leakage")
    : stats.evaluationMetrics;
  const reportPassed = stats.evaluationResult === "pass";
  const reportFailed = stats.evaluationResult === "fail";
  const judgeAudit = stats.judgeAudit;
  const judge = judgeAudit ? judgePresentation[judgeAudit.verdict] : undefined;
  const visibleJudgeAnomalies = judgeAudit?.flaggedAnomalies.slice(0, MAX_VISIBLE_JUDGE_ANOMALIES) ?? [];
  const judgeAnomaliesTruncated = Boolean(
    judgeAudit && judgeAudit.flaggedAnomalies.length > visibleJudgeAnomalies.length,
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle>Evaluation report</CardTitle>
            <CardDescription>Quality checks completed in {stats.durationSeconds}s for {stats.recordsGenerated.toLocaleString()} generated rows.</CardDescription>
          </div>
          <Badge
            className={reportPassed ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-500" : undefined}
            variant={reportFailed ? "destructive" : "outline"}
          >
            <HugeiconsIcon data-icon="inline-start" icon={reportFailed ? AlertCircleIcon : reportPassed ? CheckmarkCircle02Icon : InformationCircleIcon} strokeWidth={2} />
            {stats.evaluationResult === "partial" ? "Partial evaluation" : stats.evaluationResult}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {judgeAudit && judge ? (
          <section
            aria-labelledby="ai-judge-audit-title"
            className={`rounded-xl border p-4 ${judge.containerClassName}`}
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <HugeiconsIcon
                  className={judgeAudit.verdict === "fail" ? "mt-0.5 shrink-0 text-destructive" : "mt-0.5 shrink-0"}
                  icon={judge.icon}
                  strokeWidth={2}
                />
                <div className="min-w-0">
                  <h3 className="text-base font-semibold" id="ai-judge-audit-title">AI Judge Audit</h3>
                  <p className="mt-1 text-xs text-muted-foreground">Semantic plausibility across sampled complete workflows</p>
                </div>
              </div>
              <Badge
                className={judge.badgeClassName}
                variant={judgeAudit.verdict === "fail" ? "destructive" : "outline"}
              >
                <HugeiconsIcon data-icon="inline-start" icon={judge.icon} strokeWidth={2} />
                {judge.label}
              </Badge>
            </div>

            <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,0.35fr)_minmax(0,0.65fr)]">
              <div className="rounded-lg border bg-background/60 p-4">
                <p className="text-xs text-muted-foreground">Semantic plausibility score</p>
                <p className="mt-1 text-3xl font-semibold tabular-nums">{judgeAudit.score.toFixed(2)} <span className="text-base font-normal text-muted-foreground">/ 1.00</span></p>
                <p className="mt-3 text-xs text-muted-foreground">
                  {judgeAudit.flaggedAnomalies.length.toLocaleString()} {judgeAudit.flaggedAnomalies.length === 1 ? "anomaly" : "anomalies"} flagged
                </p>
              </div>
              <div className="rounded-lg border bg-background/60 p-4">
                <h4 className="text-sm font-medium">Judge reasoning</h4>
                <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted-foreground">{judgeAudit.reasoning}</p>
              </div>
            </div>

            <div className="mt-4 rounded-lg border bg-background/60 p-4">
              <h4 className="text-sm font-medium">Flagged anomalies ({judgeAudit.flaggedAnomalies.length})</h4>
              {judgeAnomaliesTruncated && (
                <p className="mt-2 text-xs text-muted-foreground">
                  {judgeAudit.flaggedAnomalies.length.toLocaleString()} detected, {visibleJudgeAnomalies.length.toLocaleString()} shown. All detected anomalies are included in the JSON and HTML reports.
                </p>
              )}
              {judgeAudit.flaggedAnomalies.length ? (
                <ol className="mt-3 list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
                  {visibleJudgeAnomalies.map((anomaly, index) => (
                    <li className="whitespace-pre-wrap break-words pl-1" key={`${index}-${anomaly}`}>{anomaly}</li>
                  ))}
                </ol>
              ) : (
                <p className="mt-2 text-sm text-muted-foreground">No anomalies were flagged in the sampled workflows.</p>
              )}
            </div>
          </section>
        ) : (
          <section aria-labelledby="ai-judge-audit-title" className="rounded-xl border bg-muted/20 p-4">
            <div className="flex items-start gap-3">
              <HugeiconsIcon className="mt-0.5 shrink-0 text-muted-foreground" icon={MinusSignCircleIcon} strokeWidth={2} />
              <div>
                <h3 className="text-sm font-medium" id="ai-judge-audit-title">AI Judge Audit</h3>
                <p className="mt-1 text-sm text-muted-foreground">
                  An AI Judge audit was not recorded for this run. Existing deterministic evaluation results remain available below.
                </p>
              </div>
            </div>
          </section>
        )}

        {showPrivacyFailure && privacyMetric && privacy && (
          <section className="rounded-xl border border-destructive/40 bg-destructive/5 p-4" role="alert" aria-labelledby="privacy-failure-title">
            <div className="flex items-start gap-3">
              <HugeiconsIcon className="mt-0.5 shrink-0 text-destructive" icon={AlertCircleIcon} strokeWidth={2} />
              <div>
                <Badge variant="destructive">Privacy check failed</Badge>
                <h3 className="mt-3 text-lg font-semibold" id="privacy-failure-title">
                  {privacy.evaluatedRecords
                    ? `${privacyMetric.violations.toLocaleString()} of ${privacy.evaluatedRecords} evaluated records are vulnerable`
                    : `${privacyMetric.violations.toLocaleString()} vulnerable records detected`}
                </h3>
                <p className="mt-1 text-sm text-muted-foreground">
                  {privacy.evaluatedRecords
                    ? `The privacy check evaluated ${privacy.evaluatedRecords} records from ${stats.recordsGenerated.toLocaleString()} total generated rows.`
                    : privacyMetric.detail}
                </p>
              </div>
            </div>

            <dl className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-lg border bg-background/60 p-3">
                <dt className="text-xs text-muted-foreground">Minimum group size</dt>
                <dd className="mt-1 text-xl font-semibold tabular-nums">{privacy.minimumK ? `k = ${privacy.minimumK}` : privacyMetric.value}</dd>
                {privacy.threshold && <p className="mt-1 text-xs text-destructive">Required: k ≥ {privacy.threshold}</p>}
              </div>
              <div className="rounded-lg border bg-background/60 p-3">
                <dt className="text-xs text-muted-foreground">Vulnerable records</dt>
                <dd className="mt-1 text-xl font-semibold tabular-nums">
                  {privacyMetric.violations.toLocaleString()}{privacy.evaluatedRecords ? ` of ${privacy.evaluatedRecords}` : ""}
                </dd>
                <p className="mt-1 text-xs text-muted-foreground">Records below the k threshold</p>
              </div>
              <div className="rounded-lg border bg-background/60 p-3">
                <dt className="text-xs text-muted-foreground">Privacy risk</dt>
                <dd className="mt-1 text-xl font-semibold tabular-nums">{privacy.risk ? `${privacy.risk}%` : privacyMetric.value}</dd>
                <p className="mt-1 text-xs text-muted-foreground">Vulnerable ÷ evaluated</p>
              </div>
              <div className="rounded-lg border bg-background/60 p-3">
                <dt className="text-xs text-muted-foreground">Quasi-identifiers</dt>
                <dd className="mt-1 break-words text-sm font-medium">{privacy.quasiIdentifiers ?? "See evaluation detail"}</dd>
              </div>
            </dl>

            <div className="mt-4 rounded-lg border bg-background/40 p-3">
              <h4 className="text-sm font-medium">How this was calculated</h4>
              <p className="mt-1 text-xs text-muted-foreground">{privacyMetric.detail}</p>
            </div>
          </section>
        )}

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
          <div className="border-l pl-3"><dt className="text-xs text-muted-foreground">Total violations</dt><dd className="mt-1 text-lg font-semibold tabular-nums">{stats.flaggedRecords}</dd></div>
        </dl>
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-medium">{showPrivacyFailure ? "Other quality checks" : "Quality checks"}</h3>
          {showPrivacyFailure && <span className="text-xs text-muted-foreground">Privacy result summarized above</span>}
        </div>
        <Table>
          <TableHeader>
            <TableRow><TableHead>Metric</TableHead><TableHead>Status</TableHead><TableHead>Value</TableHead><TableHead>Violations</TableHead><TableHead>Detail</TableHead></TableRow>
          </TableHeader>
          <TableBody>
            {displayedMetrics.map((metric) => {
              const status = displayStatus(metric);
              const presentation = statusPresentation[status];
              return (
                <TableRow className={metric.status === "not_evaluated" ? "bg-muted/20" : undefined} key={metric.key}>
                  <TableCell className="font-medium">{metric.label}</TableCell>
                  <TableCell>
                    <Badge className={presentation.badgeClassName} variant={status === "fail" ? "destructive" : "outline"}>
                      <HugeiconsIcon data-icon="inline-start" icon={presentation.icon} strokeWidth={2} />
                      {presentation.label}
                    </Badge>
                  </TableCell>
                  <TableCell className="tabular-nums">{metric.value}</TableCell>
                  <TableCell className="tabular-nums">{metric.violations}</TableCell>
                  <TableCell className="max-w-xs whitespace-normal text-muted-foreground">{metric.detail}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        {stats.violations && stats.violations.length > 0 && (
          <div className="flex flex-col gap-2 pt-2 border-t">
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold text-destructive">
                Causal & Semantic Violations ({stats.violations.length})
              </span>
            </div>
            <div className="rounded-md border overflow-x-auto max-h-60 overflow-y-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Table</TableHead>
                    <TableHead>Row / Instance</TableHead>
                    <TableHead>Rule</TableHead>
                    <TableHead>Error</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stats.violations.map((v, i) => (
                    <TableRow key={i}>
                      <TableCell className="font-mono text-xs">{v.table}</TableCell>
                      <TableCell className="font-mono text-xs">{String(v.row)}</TableCell>
                      <TableCell className="font-medium text-xs">{v.rule}</TableCell>
                      <TableCell className="text-xs text-muted-foreground whitespace-normal">{v.error}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </div>
        )}
      </CardContent>
      <CardFooter className="flex-wrap justify-end gap-2">
        <Button onClick={() => window.open(`/api/evaluation/${jobId}/report?format=json`, "_blank")} type="button" variant="outline">
          <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Download JSON
        </Button>
        <Button onClick={() => window.open(`/api/evaluation/${jobId}/report?format=html`, "_blank")} type="button" variant="outline">
          <HugeiconsIcon data-icon="inline-start" icon={Download01Icon} strokeWidth={2} />Download HTML
        </Button>
        <Button onClick={() => window.open(`/api/jobs/${jobId}/evaluation-report?theme=${resolvedTheme}`, "_blank")} type="button">
          <HugeiconsIcon data-icon="inline-start" icon={ArrowUpRight01Icon} strokeWidth={2} />View Evaluation Report
        </Button>
      </CardFooter>
    </Card>
  );
}
