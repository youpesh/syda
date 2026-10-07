import type { JobStats, ScenarioConfiguration } from "~/lib/studio-types";
export function RunQualitySummary({ stats, scenario }: { stats?: JobStats; scenario: ScenarioConfiguration }) {
  if (!stats) return <p className="text-xs text-muted-foreground">Row counts and validation results are not available yet.</p>;
  const failures = stats.evaluationMetrics.filter(m => m.status === "fail");
  const unchecked = stats.evaluationMetrics.filter(m => m.status === "not_evaluated");
  return <section className="space-y-3 rounded-xl border p-3" aria-label="Generation results summary">
    <h3 className="text-sm font-medium">Generation results</h3>
    <dl className="grid grid-cols-2 gap-3 text-xs"><div><dt className="text-muted-foreground">Requested scenario instances</dt><dd className="mt-1 font-medium">{scenario.recordCount.toLocaleString()}</dd></div><div><dt className="text-muted-foreground">Generated rows across tables</dt><dd className="mt-1 font-medium">{stats.recordsGenerated.toLocaleString()}</dd></div></dl>
    <p className="text-xs text-muted-foreground">One scenario instance can create rows in several tables; conditional workflow paths can produce different table counts.</p>
    <div className="max-h-52 overflow-auto"><table className="w-full text-left text-xs [&_th]:p-2 [&_td]:p-2 [&_td]:border-t"><thead><tr><th>Table</th><th>Generated rows</th></tr></thead><tbody>{Object.entries(stats.tableRowCounts).map(([name, count]) => <tr key={name}><td className="break-all">{name}</td><td>{count.toLocaleString()}</td></tr>)}</tbody></table></div>
    <p className="text-xs">Relationship integrity: {stats.referentialIntegrity}. Evaluation: {stats.evaluationResult}.</p>
    {failures.length > 0 && <div role="alert" className="text-xs text-destructive"><p className="font-medium">Validation failures</p><ul className="list-disc pl-4">{failures.map(m => <li key={m.key}>{m.label}: {m.detail} ({m.violations} violations)</li>)}</ul></div>}
    {unchecked.length > 0 && <p className="text-xs text-muted-foreground">Not evaluated: {unchecked.map(m => m.label).join(", ")}.</p>}
  </section>;
}
