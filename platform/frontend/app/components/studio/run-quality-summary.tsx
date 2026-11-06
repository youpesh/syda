import { AlertCircleIcon, CheckmarkCircle02Icon, InformationCircleIcon } from "@hugeicons/core-free-icons";
import { HugeiconsIcon } from "@hugeicons/react";

import { Badge } from "~/components/ui/badge";
import type { JobStats, ScenarioConfiguration } from "~/lib/studio-types";

type RunQualitySummaryProps = {
  stats?: JobStats;
  scenario: ScenarioConfiguration;
  presentation?: "default" | "run-detail";
};

type EvaluationMetric = JobStats["evaluationMetrics"][number];

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
    sourceName: sourceParts?.[1],
    quasiIdentifiers: sourceParts?.[2]
      .split(",")
      .map((item) => item.trim())
      .join(" + "),
  };
}

export function RunQualitySummary({
  stats,
  scenario,
  presentation = "default",
}: RunQualitySummaryProps) {
  if (!stats) return <p className="text-xs text-muted-foreground">Row counts and validation results are not available yet.</p>;

  if (presentation === "run-detail") {
    return (
      <section className="rounded-xl border p-4" aria-label="Generation results summary">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="text-sm font-medium">Data generation complete</h3>
            <p className="mt-1 text-xs text-muted-foreground">
              {scenario.recordCount.toLocaleString()} requested scenario instances generated {stats.recordsGenerated.toLocaleString()} rows across {Object.keys(stats.tableRowCounts).length.toLocaleString()} tables.
            </p>
          </div>
          <dl className="grid shrink-0 grid-cols-2 gap-x-8 gap-y-3 text-xs">
            <div>
              <dt className="text-muted-foreground">Generated rows</dt>
              <dd className="mt-1 text-lg font-semibold tabular-nums">{stats.recordsGenerated.toLocaleString()}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Scenario instances</dt>
              <dd className="mt-1 text-lg font-semibold tabular-nums">{scenario.recordCount.toLocaleString()}</dd>
            </div>
          </dl>
        </div>
      </section>
    );
  }

  const failures = stats.evaluationMetrics.filter(m => m.status === "fail");
  const unchecked = stats.evaluationMetrics.filter(m => m.status === "not_evaluated");
  const privacyMetric = stats.evaluationMetrics.find((metric) => metric.key === "privacy_leakage");
  const privacyFailed = privacyMetric?.status === "fail";
  const privacyPassed = privacyMetric?.status === "pass";
  const otherFailures = failures.filter((metric) => metric.key !== "privacy_leakage");
  const privacy = privacyMetric ? privacySummary(privacyMetric) : undefined;
  const integrityPassed = stats.referentialIntegrity === "100%" || stats.referentialIntegrity === "100.0%";
  const evaluationLabel = stats.evaluationResult === "partial" ? "Partial evaluation" : `Evaluation ${stats.evaluationResult}`;

  return <section className="space-y-3 rounded-xl border p-3" aria-label="Generation results summary">
    <h3 className="text-sm font-medium">Generation results</h3>
    <dl className="grid grid-cols-2 gap-3 text-xs"><div><dt className="text-muted-foreground">Requested scenario instances</dt><dd className="mt-1 font-medium">{scenario.recordCount.toLocaleString()}</dd></div><div><dt className="text-muted-foreground">Generated rows across tables</dt><dd className="mt-1 font-medium">{stats.recordsGenerated.toLocaleString()}</dd></div></dl>
    <p className="text-xs text-muted-foreground">One scenario instance can create rows in several tables; conditional workflow paths can produce different table counts.</p>
    <div className="max-h-52 overflow-auto"><table className="w-full text-left text-xs [&_th]:p-2 [&_td]:p-2 [&_td]:border-t"><thead><tr><th>Table</th><th>Generated rows</th></tr></thead><tbody>{Object.entries(stats.tableRowCounts).map(([name, count]) => <tr key={name}><td className="break-all">{name}</td><td>{count.toLocaleString()}</td></tr>)}</tbody></table></div>
    <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-xs">
      <span className={`inline-flex items-center gap-1.5 font-medium ${integrityPassed ? "text-foreground" : "text-destructive"}`}>
        <HugeiconsIcon
          className={integrityPassed ? "text-emerald-500" : "text-destructive"}
          icon={integrityPassed ? CheckmarkCircle02Icon : AlertCircleIcon}
          size={18}
          strokeWidth={2}
        />
        Relationship integrity {stats.referentialIntegrity}
      </span>
      <Badge variant={stats.evaluationResult === "fail" ? "destructive" : stats.evaluationResult === "pass" ? "secondary" : "outline"}>
        {stats.evaluationResult === "partial" && <HugeiconsIcon data-icon="inline-start" icon={InformationCircleIcon} strokeWidth={2} />}
        {evaluationLabel}
      </Badge>
    </div>

    {privacyFailed && privacyMetric && privacy && (
      <div className="flex flex-col gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs sm:flex-row sm:items-center sm:justify-between" role="alert">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 font-medium text-destructive">
            <HugeiconsIcon className="shrink-0" icon={AlertCircleIcon} size={18} strokeWidth={2} />
            Privacy leakage risk
          </p>
          <p className="mt-1 font-medium">
            {privacy.evaluatedRecords
              ? `${privacyMetric.violations.toLocaleString()} of ${privacy.evaluatedRecords} ${privacy.sourceName ?? "evaluated"} records are vulnerable`
              : `${privacyMetric.violations.toLocaleString()} vulnerable records detected`}
          </p>
          {privacy.quasiIdentifiers && <p className="mt-1 text-muted-foreground">Quasi-identifiers: {privacy.quasiIdentifiers}</p>}
        </div>
        <dl className="flex shrink-0 items-center gap-3 border-destructive/30 sm:border-l sm:pl-3">
          <div>
            <dt className="sr-only">Minimum group size</dt>
            <dd className="font-semibold text-destructive">{privacy.minimumK ? `k = ${privacy.minimumK}` : privacyMetric.value}</dd>
          </div>
          {privacy.threshold && (
            <div>
              <dt className="sr-only">Required group size</dt>
              <dd className="text-muted-foreground">Required k ≥ {privacy.threshold}</dd>
            </div>
          )}
        </dl>
      </div>
    )}

    {privacyPassed && privacyMetric && privacy && (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" role="status">
        <span className="inline-flex items-center gap-1.5 font-medium text-emerald-500">
          <HugeiconsIcon icon={CheckmarkCircle02Icon} size={18} strokeWidth={2} />
          Privacy check passed
        </span>
        <span aria-hidden="true" className="hidden h-4 w-px bg-border sm:block" />
        <span className="font-medium">{privacy.minimumK ? `k = ${privacy.minimumK}` : privacyMetric.value}</span>
        {privacy.risk && <><span aria-hidden="true" className="hidden h-4 w-px bg-border sm:block" /><span className="text-muted-foreground">Risk {privacy.risk}%</span></>}
        {privacy.quasiIdentifiers && <><span aria-hidden="true" className="hidden h-4 w-px bg-border sm:block" /><span className="text-muted-foreground">{privacy.quasiIdentifiers}</span></>}
      </div>
    )}

    {otherFailures.length > 0 && (
      <div className="text-xs text-destructive" role="alert">
        <span className="font-medium">Other validation failures: </span>
        {otherFailures.map((metric) => `${metric.label} (${metric.violations})`).join(" · ")}
      </div>
    )}
    {unchecked.length > 0 && <p className="text-xs text-muted-foreground">Not evaluated: {unchecked.map(m => m.label).join(" · ")}</p>}
  </section>;
}
