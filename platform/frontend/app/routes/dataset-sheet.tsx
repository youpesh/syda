import { useQuery } from "@tanstack/react-query";
import { useState, type KeyboardEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { cn } from "cn";

import type { Route } from "./+types/dataset-sheet";
import { AppShell } from "~/components/studio/app-shell";
import { Button, buttonVariants } from "~/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "~/components/ui/empty";
import { Spinner } from "~/components/ui/spinner";
import type { GenerationJob, SavedScenario, SchemaTable } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) { return [{ title: "Dataset — Syda" }]; }

const PAGE_SIZE = 50;
// ponytail: syda's default instance_id_field; scenarios can't override it yet. Violations name rows by it.
const INSTANCE = "scenario_instance_id";

type Row = Record<string, unknown>;
type Page = { columns: string[]; rows: Row[]; rowCount: number };
type Filter = { column: string; value: string };
type RowRef = Filter & { table: string };
type FieldDef = { type?: string; description?: string; constraints?: Record<string, unknown> };

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    const body = await response.json().catch(() => undefined) as { detail?: unknown } | undefined;
    throw new Error(typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function usePage(jobId: string | undefined, table: string | undefined, filter?: Filter, offset = 0, limit = PAGE_SIZE) {
  const params = new URLSearchParams({ table: table ?? "", limit: String(limit), offset: String(offset), ...filter });
  return useQuery<Page, Error>({
    queryKey: ["sheet-page", jobId, params.toString()],
    enabled: Boolean(jobId && table),
    staleTime: 60_000,
    queryFn: async ({ signal }) => Object.values((await getJson<{ tables: Record<string, Page> }>(`/api/preview/${jobId}?${params}`, signal)).tables)[0],
  });
}

const text = (value: unknown) => value == null ? "—" : String(value);
const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

function fieldDef(schema: SchemaTable | undefined, column: string): FieldDef | undefined {
  const def = schema?.[column];
  return typeof def === "string" ? { type: def } : def && typeof def === "object" ? def as FieldDef : undefined;
}

// __foreign_keys__ values are "Table.column", [table, column] or {table, column}.
function foreignKeys(schema: SchemaTable | undefined): Record<string, { table: string; column: string }> {
  const raw = (schema?.__foreign_keys__ ?? {}) as Record<string, unknown>;
  return Object.fromEntries(Object.entries(raw).flatMap(([column, target]) => {
    const [table, parent] = typeof target === "string" ? target.split(".", 2)
      : Array.isArray(target) ? target
      : target && typeof target === "object" ? [(target as { table?: unknown }).table, (target as { column?: unknown }).column] : [];
    return table && parent ? [[column, { table: String(table), column: String(parent) }]] : [];
  }));
}

function primaryKey(schema: SchemaTable | undefined, columns: string[]) {
  return columns.find((column) => fieldDef(schema, column)?.constraints?.primary_key) ?? columns[0];
}

// Parents before children, the order tables are generated in.
function generationOrder(schemas: Record<string, SchemaTable>) {
  const names = Object.keys(schemas);
  const order: string[] = [];
  const visit = (name: string, path: string[]) => {
    if (order.includes(name) || path.includes(name)) return;
    for (const fk of Object.values(foreignKeys(schemas[name]))) {
      const parent = names.find((candidate) => same(candidate, fk.table));
      if (parent) visit(parent, [...path, name]);
    }
    order.push(name);
  };
  names.forEach((name) => visit(name, []));
  return order;
}

function summary(row: Row, skip: string[]) {
  return Object.entries(row).filter(([column, value]) => !skip.includes(column) && column !== INSTANCE && column !== "scenario_path" && value != null).slice(0, 2).map(([, value]) => text(value)).join(" · ");
}

function LinkedRows({ jobId, refTo, pk, onOpen, onShowAll }: { jobId: string; refTo: RowRef; pk: (table: string, columns: string[]) => string; onOpen: (ref: RowRef) => void; onShowAll: (ref: RowRef) => void }) {
  const page = usePage(jobId, refTo.table, refTo, 0, 5);
  if (page.isPending) return <p className="text-xs text-muted-foreground">Loading {refTo.table}…</p>;
  if (page.isError) return <p className="text-xs text-destructive">{refTo.table}: {page.error.message}</p>;
  if (!page.data.rows.length) return <p className="text-xs text-muted-foreground">No {refTo.table} rows link here.</p>;
  const key = pk(refTo.table, page.data.columns);
  return <>
    {page.data.rows.map((row, index) => <button className="flex min-h-9 items-baseline gap-2.5 rounded-md bg-muted/50 px-2 py-1.5 text-left text-xs hover:bg-muted" key={index} onClick={() => onOpen({ table: refTo.table, column: key, value: text(row[key]) })} type="button">
      <span className="min-w-20 font-semibold uppercase tracking-wide text-muted-foreground">{refTo.table}</span>
      <span className="font-mono text-primary">{text(row[key])}</span>
      <span className="truncate">{summary(row, [key, refTo.column])}</span>
    </button>)}
    {page.data.rowCount > page.data.rows.length && <Button className="self-start" onClick={() => onShowAll(refTo)} size="xs" variant="ghost">All {page.data.rowCount.toLocaleString()} in the {refTo.table} sheet</Button>}
  </>;
}

export default function DatasetSheet() {
  const { scenarioId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<string>();
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<Filter>();
  const [panel, setPanel] = useState<RowRef>();
  const [ruleCursor, setRuleCursor] = useState<Record<string, number>>({});

  const scenarioQuery = useQuery<SavedScenario, Error>({ queryKey: ["scenario", scenarioId], queryFn: ({ signal }) => getJson(`/api/scenarios/${scenarioId}`, signal) });
  const jobsQuery = useQuery<GenerationJob[], Error>({ queryKey: ["jobs", "sheet"], queryFn: ({ signal }) => getJson("/api/jobs?limit=200", signal) });
  const runs = (jobsQuery.data ?? []).filter((job) => job.scenarioId === scenarioId && job.status === "complete" && job.filesAvailable);
  const run = runs.find((job) => job.jobId === searchParams.get("run")) ?? runs[0];
  const schemas = run?.scenario?.schemas ?? scenarioQuery.data?.configuration.schemas ?? {};
  const tables = generationOrder(schemas);
  const activeTab = tab && tables.includes(tab) ? tab : tables[0];
  const resolve = (name: string) => tables.find((table) => same(table, name)) ?? name;
  const page = usePage(run?.jobId, activeTab, filter, offset);
  const pk = (table: string, columns: string[]) => primaryKey(schemas[resolve(table)], columns);

  const stats = run?.stats;
  const metrics = (stats?.evaluationMetrics ?? []).filter((metric) => metric.status !== "not_evaluated");
  const orphaned = stats?.evaluationMetrics.find((metric) => metric.key === "referential_integrity")?.violations ?? 0;
  const failing = new Map<string, { rule: string; error: string }[]>();
  const rules = new Map<string, { table: string; row: string }[]>();
  for (const violation of stats?.violations ?? []) {
    const key = `${violation.table.toLowerCase()}|${violation.row}`;
    failing.set(key, [...(failing.get(key) ?? []), violation]);
    rules.set(violation.rule, [...(rules.get(violation.rule) ?? []), { table: violation.table, row: String(violation.row) }]);
  }
  const failuresOf = (table: string, row: Row, index?: number) => failing.get(`${table.toLowerCase()}|${row[INSTANCE] ?? index}`) ?? [];

  const show = (table: string, nextFilter?: Filter) => { setTab(table); setFilter(nextFilter); setOffset(0); };
  const openRow = (ref: RowRef) => { const table = resolve(ref.table); show(table, ref); setPanel({ ...ref, table }); };
  const jumpToRule = (rule: string) => {
    const targets = (rules.get(rule) ?? []).filter((target) => target.row !== "N/A");
    if (!targets.length) return;
    const index = (ruleCursor[rule] ?? 0) % targets.length;
    setRuleCursor({ ...ruleCursor, [rule]: index + 1 });
    show(resolve(targets[index].table), { column: INSTANCE, value: targets[index].row });
    setPanel(undefined);
  };
  const pickRun = (jobId: string) => { setSearchParams({ run: jobId }); setFilter(undefined); setOffset(0); setPanel(undefined); };

  const moveFocus = (event: KeyboardEvent<HTMLTableElement>) => {
    const delta = ({ ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] } as Record<string, [number, number]>)[event.key];
    const cell = (event.target as HTMLElement).closest("td");
    if (!delta || !cell) return;
    const row = cell.parentElement as HTMLTableRowElement;
    const target = row.parentElement?.children[row.sectionRowIndex + delta[0]]?.children[cell.cellIndex + delta[1]] as HTMLElement | undefined;
    if (!target) return;
    event.preventDefault();
    (target.querySelector("button") ?? target).focus();
  };

  const loadError = scenarioQuery.error ?? jobsQuery.error;
  const sourcePicker = runs.length > 0 && <>
    <label className="sr-only" htmlFor="sheet-source">Data source</label>
    <select className="h-8 rounded-md border bg-background px-2 text-xs" id="sheet-source" onChange={(event) => pickRun(event.target.value)} value={run?.jobId}>
      {runs.map((job, index) => <option key={job.jobId} value={job.jobId}>{index === 0 ? "Latest · " : ""}{job.createdAt ? new Date(job.createdAt).toLocaleString() : job.jobId} · {job.stats?.recordsGenerated?.toLocaleString() ?? "?"} rows</option>)}
    </select>
  </>;

  return <AppShell title={scenarioQuery.data?.title ?? "Dataset"} subtitle="Dataset" headerActions={<div className="flex items-center gap-2">{sourcePicker}<Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/scenarios/${scenarioId}`}>Edit scenario</Link></div>}>
    {loadError ? <p className="p-6 text-sm text-destructive" role="alert">{loadError.message}</p>
      : scenarioQuery.isPending || jobsQuery.isPending ? <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground"><Spinner /> Loading dataset…</div>
      : !run ? <Empty className="p-6"><EmptyHeader><EmptyTitle>No generated data yet</EmptyTitle><EmptyDescription>Generate this dataset from the scenario editor, then come back to browse every sheet.</EmptyDescription></EmptyHeader><Link className={buttonVariants({})} to={`/scenarios/${scenarioId}`}>Open scenario</Link></Empty>
      : <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <section aria-label={`${activeTab} sheet`} className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-auto bg-background">
            <table className="min-w-full border-separate border-spacing-0 text-xs" onKeyDown={moveFocus}>
              <thead><tr>
                <th className="sticky top-0 z-10 w-11 border-r border-b bg-muted p-2 font-medium text-muted-foreground" scope="col">#</th>
                {page.data?.columns.map((column) => {
                  const def = fieldDef(schemas[activeTab], column);
                  const fk = foreignKeys(schemas[activeTab])[column];
                  return <th className="sticky top-0 z-10 min-w-36 max-w-56 border-r border-b bg-muted px-3 py-2 text-left align-top font-normal" key={column} scope="col">
                    <span className="block font-semibold text-foreground">{column}</span>
                    <span className={cn("block font-mono text-[11px]", fk ? "text-primary" : "text-muted-foreground")}>{fk ? `→ ${resolve(fk.table)}` : def?.type ?? "—"}</span>
                    {def?.description && <span className="mt-0.5 line-clamp-2 block text-[11px] leading-snug whitespace-normal text-muted-foreground" title={def.description}>{def.description}</span>}
                  </th>;
                })}
              </tr></thead>
              <tbody>{page.data?.rows.map((row, index) => {
                const number = offset + index;
                const failures = failuresOf(activeTab, row, filter ? undefined : number);
                const key = pk(activeTab, page.data.columns);
                const open = panel?.table === activeTab && text(row[panel.column]) === panel.value;
                const fks = foreignKeys(schemas[activeTab]);
                return <tr className={cn(open && "bg-primary/5")} key={number}>
                  <td className="border-r border-b p-0 text-center">
                    <button aria-label={`Open row ${number + 1} details${failures.length ? `, fails ${failures.map((failure) => failure.rule).join(", ")}` : ""}`} className={cn("min-h-9 min-w-11 text-[11px]", failures.length ? "font-semibold text-destructive" : "text-muted-foreground")} onClick={() => setPanel({ table: activeTab, column: key, value: text(row[key]) })} title={failures.map((failure) => failure.error).join("\n") || undefined} type="button">{number + 1}</button>
                  </td>
                  {page.data.columns.map((column) => {
                    const fk = fks[column];
                    const value = row[column];
                    return <td className={cn("max-w-64 truncate border-r border-b px-3 py-1.5 focus:outline-2 focus:outline-ring", failures.length && "bg-destructive/10", column === key && "font-mono text-muted-foreground")} key={column} tabIndex={-1} title={text(value)}>
                      {fk && value != null
                        ? <button aria-label={`Open ${resolve(fk.table)} ${text(value)}`} className="-ml-1.5 min-h-7 rounded-md px-1.5 text-primary underline underline-offset-2" onClick={() => openRow({ table: fk.table, column: fk.column, value: text(value) })} type="button">{text(value)}</button>
                        : text(value)}
                    </td>;
                  })}
                </tr>;
              })}</tbody>
            </table>
            <div className="flex flex-wrap items-center gap-2 px-3.5 py-2.5 text-xs text-muted-foreground">
              {page.isPending ? <><Spinner /> Loading {activeTab}…</>
                : page.isError ? <span className="text-destructive" role="alert">{page.error.message}</span>
                : <span>Rows {page.data.rows.length ? offset + 1 : 0}–{offset + page.data.rows.length} of {page.data.rowCount.toLocaleString()}</span>}
              {filter && <Button onClick={() => show(activeTab)} size="xs" variant="outline">{filter.column} = {filter.value} · Show all</Button>}
              <span className="flex-1" />
              <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} size="xs" variant="outline">Previous</Button>
              <Button disabled={!page.data || offset + PAGE_SIZE >= page.data.rowCount} onClick={() => setOffset(offset + PAGE_SIZE)} size="xs" variant="outline">Next</Button>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-x-1 gap-y-1.5 border-t bg-muted/50 px-3 py-1.5">
            <div aria-label="Sheets, in generation order" className="flex flex-wrap gap-0.5" role="tablist">
              {tables.map((table) => {
                const count = Object.entries(stats?.tableRowCounts ?? {}).find(([name]) => same(name, table))?.[1];
                return <button aria-selected={table === activeTab} className={cn("min-h-9 rounded-md border px-3 text-xs", table === activeTab ? "border-border bg-background font-semibold" : "border-transparent text-muted-foreground hover:text-foreground")} key={table} onClick={() => show(table)} role="tab" type="button">
                  {table} <span className="font-normal text-muted-foreground">{count?.toLocaleString()}</span>
                </button>;
              })}
            </div>
            <span className="flex-1" />
            <span className={cn("text-xs font-semibold", rules.size || orphaned ? "text-destructive" : "text-primary")}>
              {metrics.filter((metric) => metric.status === "pass").length}/{metrics.length} checks pass · {orphaned.toLocaleString()} orphaned rows
            </span>
            {[...rules].map(([rule, targets]) => <Button key={rule} onClick={() => jumpToRule(rule)} size="xs" title="Jump to the next failing row" variant="ghost">
              <span className="text-destructive">{rule}</span> <span className="text-muted-foreground">{targets.length}</span>
            </Button>)}
          </div>
        </section>
        {panel && <RowPanel jobId={run.jobId} onClose={() => setPanel(undefined)} onOpen={openRow} onShowAll={(ref) => { show(resolve(ref.table), ref); setPanel(undefined); }} pk={pk} refTo={panel} schemas={schemas} tables={tables} failuresOf={failuresOf} />}
      </div>}
  </AppShell>;
}

function RowPanel({ jobId, refTo, schemas, tables, pk, failuresOf, onOpen, onShowAll, onClose }: {
  jobId: string; refTo: RowRef; schemas: Record<string, SchemaTable>; tables: string[];
  pk: (table: string, columns: string[]) => string;
  failuresOf: (table: string, row: Row) => { rule: string; error: string }[];
  onOpen: (ref: RowRef) => void; onShowAll: (ref: RowRef) => void; onClose: () => void;
}) {
  const page = usePage(jobId, refTo.table, refTo, 0, 1);
  const row = page.data?.rows[0];
  const parents = Object.entries(foreignKeys(schemas[refTo.table])).filter(([column]) => row?.[column] != null)
    .map(([column, fk]) => ({ table: fk.table, column: fk.column, value: text(row?.[column]) }));
  const children = tables.flatMap((table) => Object.entries(foreignKeys(schemas[table]))
    .filter(([, fk]) => same(fk.table, refTo.table) && row?.[fk.column] != null)
    .map(([column, fk]) => ({ table, column, value: text(row?.[fk.column]) })));
  const failures = row ? failuresOf(refTo.table, row) : [];
  const linked = (title: string, refs: RowRef[]) => refs.length > 0 && <div className="flex flex-col gap-1">
    <h3 className="text-xs font-semibold">{title}</h3>
    {refs.map((ref) => <LinkedRows jobId={jobId} key={`${ref.table}.${ref.column}`} onOpen={onOpen} onShowAll={onShowAll} pk={pk} refTo={ref} />)}
  </div>;

  return <aside aria-label="Row details" className="flex max-h-[50vh] w-full shrink-0 flex-col gap-4 overflow-auto border-t bg-background p-4 lg:max-h-none lg:w-96 lg:border-t-0 lg:border-l">
    <div className="flex items-start justify-between gap-2">
      <div><p className="text-xs text-muted-foreground">{refTo.table} row</p><h2 className="font-mono text-base">{refTo.table} {refTo.value}</h2></div>
      <Button aria-label="Close panel" onClick={onClose} size="icon-sm" variant="ghost">×</Button>
    </div>
    {page.isPending ? <p className="flex items-center gap-2 text-xs text-muted-foreground"><Spinner /> Loading row…</p>
      : page.isError ? <p className="text-xs text-destructive" role="alert">{page.error.message}</p>
      : !row ? <p className="text-xs text-muted-foreground">This row is not in the selected run.</p>
      : <>
        {failures.length > 0 && <ul className="flex flex-col gap-1 rounded-md bg-destructive/10 p-2.5 text-xs text-destructive">{failures.map((failure, index) => <li key={index}><strong>{failure.rule}:</strong> {failure.error}</li>)}</ul>}
        <dl className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1.4fr)] gap-x-2.5 gap-y-1 text-xs">
          {Object.entries(row).map(([column, value]) => <div className="contents" key={column}><dt className="truncate text-muted-foreground">{column}</dt><dd className="break-words">{text(value)}</dd></div>)}
        </dl>
        {linked("Linked parent rows", parents)}
        {linked("Child rows", children)}
      </>}
  </aside>;
}
