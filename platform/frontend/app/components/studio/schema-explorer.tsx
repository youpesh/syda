import { useState } from "react";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import type { SchemaTable } from "~/lib/studio-types";

export function SchemaExplorer({ schemas }: { schemas: Record<string, SchemaTable> }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState("");
  const [columnQuery, setColumnQuery] = useState("");
  const [page, setPage] = useState(0);
  const names = Object.keys(schemas);
  const name = names.includes(selected) ? selected : names[0];
  const fields = Object.entries(schemas[name] ?? {}).filter(([key]) => !key.startsWith("__"));
  const columns = fields.filter(([key, value]) => `${key} ${JSON.stringify(value)}`.toLowerCase().includes(columnQuery.toLowerCase()));
  const pages = Math.max(1, Math.ceil(columns.length / 20));
  const current = Math.min(page, pages - 1);
  return <div className="space-y-3 min-w-0">
    <p className="text-xs text-muted-foreground">{names.length} tables · Structure only</p>
    <div className="grid min-w-0 gap-4 sm:grid-cols-[160px_minmax(0,1fr)]">
      <div className="min-w-0"><Input aria-label="Search schema tables" placeholder="Search tables" value={query} onChange={e => setQuery(e.target.value)} />
        <div className="mt-2 flex max-h-64 flex-col gap-1 overflow-y-auto">{names.filter(n => n.toLowerCase().includes(query.toLowerCase())).map(n => <Button type="button" key={n} title={n} className="w-full min-w-0 justify-start" variant={name === n ? "secondary" : "ghost"} aria-pressed={name === n} onClick={() => { setSelected(n); setColumnQuery(""); setPage(0); }}><span className="truncate">{n}</span></Button>)}{!names.some(n => n.toLowerCase().includes(query.toLowerCase())) && <p className="text-xs p-2">No matching tables.</p>}</div>
      </div>
      <div className="min-w-0"><h4 className="mb-2 break-all text-sm font-medium">{name ?? "No tables found"}</h4><Input aria-label="Search schema columns" placeholder="Search columns or constraints" value={columnQuery} onChange={e => { setColumnQuery(e.target.value); setPage(0); }} />
        <div className="mt-2 max-h-64 overflow-auto rounded-xl border"><table className="w-full text-left text-xs [&_th]:p-2 [&_td]:p-2 [&_td]:border-t"><thead className="sticky top-0 bg-card"><tr><th>Column</th><th>Definition / constraints</th></tr></thead><tbody>{columns.slice(current * 20, (current + 1) * 20).map(([key, value]) => <tr key={key}><td className="align-top break-all">{key}</td><td className="max-w-64 whitespace-pre-wrap break-words">{typeof value === "string" ? value : JSON.stringify(value)}</td></tr>)}</tbody></table>{!columns.length && <p className="p-3 text-xs">No matching columns.</p>}</div>
        <div className="mt-3 flex flex-wrap items-center gap-2"><Button type="button" size="sm" variant="outline" disabled={!current} onClick={() => setPage(current - 1)}>Previous</Button><span className="text-xs">Page {current + 1} of {pages} · {columns.length} columns</span><Button type="button" size="sm" variant="outline" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>Next</Button></div>
      </div>
    </div>
  </div>;
}
