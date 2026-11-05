import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { ArrowRight, Check, Database, GitBranch, Menu, Plus, X } from "lucide-react";

import type { Route } from "./+types/landing";
import { buttonVariants } from "~/components/ui/button";
import { ThemeToggle } from "~/components/ui/theme-toggle";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Syda — Synthetic data. Real-world logic." },
    { name: "description", content: "Generate linked synthetic test datasets from your business rules. Design scenarios, inspect relationships, and export CSV or JSON without using customer records." },
  ];
}

// A curated synthetic example. Every child key resolves to a record in this sample.
const patients = [
  { patient_id: "PT-00421", age: 34, care_path: "Routine follow-up" },
  { patient_id: "PT-00422", age: 67, care_path: "Escalated care" },
  { patient_id: "PT-00423", age: 42, care_path: "Routine follow-up" },
];
const visits = [
  { visit_id: "VS-01934", patient_id: "PT-00421", visit_date: "2026-10-12", type: "Consultation" },
  { visit_id: "VS-01935", patient_id: "PT-00422", visit_date: "2026-10-13", type: "Consultation" },
  { visit_id: "VS-01936", patient_id: "PT-00423", visit_date: "2026-10-14", type: "Consultation" },
  { visit_id: "VS-01937", patient_id: "PT-00421", visit_date: "2026-10-26", type: "Follow-up" },
  { visit_id: "VS-01938", patient_id: "PT-00422", visit_date: "2026-10-20", type: "Specialist" },
];
const diagnoses = [
  { diagnosis_id: "DX-00831", visit_id: "VS-01934", diagnosis: "Seasonal allergy" },
  { diagnosis_id: "DX-00832", visit_id: "VS-01935", diagnosis: "Hypertension" },
  { diagnosis_id: "DX-00833", visit_id: "VS-01936", diagnosis: "Migraine" },
];
const treatments = [
  { treatment_id: "TX-00611", diagnosis_id: "DX-00831", treatment: "Allergy management" },
  { treatment_id: "TX-00612", diagnosis_id: "DX-00832", treatment: "Specialist referral" },
  { treatment_id: "TX-00613", diagnosis_id: "DX-00833", treatment: "Migraine management" },
];
const sample = { patients, visits, diagnoses, treatments };
const checks = [
  { label: "Patient references", pass: visits.every((v) => patients.some((p) => p.patient_id === v.patient_id)) },
  { label: "Visit references", pass: diagnoses.every((d) => visits.some((v) => v.visit_id === d.visit_id)) },
  { label: "Diagnosis references", pass: treatments.every((t) => diagnoses.some((d) => d.diagnosis_id === t.diagnosis_id)) },
  { label: "Unique record IDs", pass: Object.values(sample).every((rows) => new Set(rows.map((row) => Object.values(row)[0])).size === rows.length) },
];

type DemoRecord = {
  id: string;
  kind: "Patient" | "Visit" | "Diagnosis" | "Treatment";
  title: string;
  detail?: string;
  parentId?: string;
  parentKind?: string;
  fields: { label: string; value: string; reference?: string }[];
};
type RecordEdge = { from: string; to: string; path: string };

function formatVisitDate(date: string) {
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(date));
}

function DatasetDemo() {
  const [patientId, setPatientId] = useState(patients[0].patient_id);
  const [selectedId, setSelectedId] = useState(visits[0].visit_id);
  const mapRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState<RecordEdge[]>([]);
  const patient = patients.find((p) => p.patient_id === patientId)!;
  const linkedVisits = visits.filter((v) => v.patient_id === patientId);
  const linkedDiagnoses = diagnoses.filter((d) => linkedVisits.some((v) => v.visit_id === d.visit_id));
  const linkedTreatments = treatments.filter((t) => linkedDiagnoses.some((d) => d.diagnosis_id === t.diagnosis_id));
  const records: DemoRecord[] = [
    { id: patient.patient_id, kind: "Patient", title: patient.patient_id, detail: `Age ${patient.age}`, fields: [{ label: "Patient ID", value: patient.patient_id }, { label: "Age", value: String(patient.age) }, { label: "Care path", value: patient.care_path }] },
    ...linkedVisits.map((v): DemoRecord => ({ id: v.visit_id, kind: "Visit", title: v.type, detail: formatVisitDate(v.visit_date), parentId: v.patient_id, parentKind: "Patient", fields: [{ label: "Visit ID", value: v.visit_id }, { label: "Patient", value: v.patient_id, reference: v.patient_id }, { label: "Date", value: formatVisitDate(v.visit_date) }, { label: "Visit type", value: v.type }] })),
    ...linkedDiagnoses.map((d): DemoRecord => ({ id: d.diagnosis_id, kind: "Diagnosis", title: d.diagnosis, parentId: d.visit_id, parentKind: "Visit", fields: [{ label: "Diagnosis ID", value: d.diagnosis_id }, { label: "Visit", value: d.visit_id, reference: d.visit_id }, { label: "Diagnosis", value: d.diagnosis }] })),
    ...linkedTreatments.map((t): DemoRecord => ({ id: t.treatment_id, kind: "Treatment", title: t.treatment, parentId: t.diagnosis_id, parentKind: "Diagnosis", fields: [{ label: "Treatment ID", value: t.treatment_id }, { label: "Diagnosis", value: t.diagnosis_id, reference: t.diagnosis_id }, { label: "Treatment", value: t.treatment }] })),
  ];
  const selected = records.find((record) => record.id === selectedId) ?? records[0];
  const selectRecord = (id: string) => {
    setSelectedId(id);
    if (window.matchMedia("(max-width: 760px)").matches) {
      const inspector = document.getElementById("record-inspector");
      inspector?.focus({ preventScroll: true });
      inspector?.scrollIntoView({ block: "start" });
    }
  };
  const linkCount = records.filter((record) => record.parentId).length;
  const referencesResolve = records.every((record) => !record.parentId || records.some((parent) => parent.id === record.parentId));

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    // Connect actual record endpoints, including both visits and their true parents.
    const draw = () => {
      const bounds = map.getBoundingClientRect();
      const next: RecordEdge[] = [];
      map.querySelectorAll<HTMLElement>("[data-parent-id]").forEach((child) => {
        const parentId = child.dataset.parentId;
        const parent = [...map.querySelectorAll<HTMLElement>("[data-record-id]")].find((node) => node.dataset.recordId === parentId);
        if (!parent || !parentId) return;
        const a = parent.getBoundingClientRect();
        const b = child.getBoundingClientRect();
        const x1 = a.right - bounds.left, y1 = a.top + a.height / 2 - bounds.top;
        const x2 = b.left - bounds.left, y2 = b.top + b.height / 2 - bounds.top;
        const middle = (x1 + x2) / 2;
        next.push({ from: parentId, to: child.dataset.recordId!, path: `M ${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` });
      });
      setEdges(next);
    };
    draw();
    if (!("ResizeObserver" in window)) return;
    const observer = new ResizeObserver(draw);
    observer.observe(map);
    map.querySelectorAll("[data-record-id]").forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [patientId]);

  return <div className="dataset-demo" data-ui>
    <div className="dataset-toolbar">
      <div className="dataset-identity"><GitBranch aria-hidden="true" /><div><strong>Patient care</strong><span>Interactive synthetic scenario</span></div></div>
      <div className="dataset-patient-picker"><label htmlFor="sample-patient">Patient</label><select id="sample-patient" value={patientId} onChange={(event) => {
        const nextPatient = event.target.value;
        setPatientId(nextPatient);
        setSelectedId(visits.find((visit) => visit.patient_id === nextPatient)?.visit_id ?? nextPatient);
      }}>{patients.map((p) => <option key={p.patient_id}>{p.patient_id}</option>)}</select></div>
    </div>
    <div className="record-workbench">
      <div className="record-map" ref={mapRef} data-illustration role="group" aria-label={`Connected records for ${patientId}`}>
        <svg className="record-map-links" aria-hidden="true">{edges.map((edge) => <path key={`${edge.from}-${edge.to}`} d={edge.path} data-active={selected.id === edge.from || selected.id === edge.to} />)}</svg>
        {(["Patient", "Visit", "Diagnosis", "Treatment"] as const).map((kind) => <div className="record-column" key={kind}>
          <div className="record-column-title">{kind === "Visit" ? "Visits" : kind}</div>
          <div className="record-node-stack">{records.filter((record) => record.kind === kind).map((record) => <button type="button" className="record-node" key={record.id} data-record-id={record.id} data-parent-id={record.parentId} aria-pressed={selected.id === record.id} aria-label={`Inspect ${record.kind.toLowerCase()} ${record.id}: ${record.title}`} aria-controls="record-inspector" onClick={() => selectRecord(record.id)}>
            <strong>{record.title}</strong>
            {record.kind !== "Patient" && <span>{record.id}</span>}
            {record.detail && <span>{record.detail}</span>}
            {record.parentId && <span className="record-mobile-parent">{record.parentKind} · {record.parentId}</span>}
          </button>)}</div>
        </div>)}
      </div>
      <aside className="record-inspector" id="record-inspector" tabIndex={-1} aria-label="Selected record details" aria-live="polite" aria-atomic="true">
        <div className="record-inspector-heading"><span>Record details</span><Database aria-hidden="true" /></div>
        <h3>{selected.kind}</h3>
        <dl>{selected.fields.map((field) => <div key={field.label}><dt>{field.label}</dt><dd>{field.reference ? <button type="button" className="record-reference" onClick={() => selectRecord(field.reference!)} aria-label={`Inspect linked ${field.label.toLowerCase()} ${field.value}`}>{field.value}<ArrowRight aria-hidden="true" /></button> : field.value}</dd></div>)}</dl>
        <p className="record-inspector-note">{selected.parentId ? `This ${selected.kind.toLowerCase()} belongs to ${selected.parentId}.` : `${linkedVisits.length} ${linkedVisits.length === 1 ? "visit is" : "visits are"} linked to this patient.`}</p>
      </aside>
    </div>
    <div className="dataset-status"><span>{patient.care_path}<span aria-hidden="true"> · </span>{records.length} records<span aria-hidden="true"> · </span>{linkCount} links</span><span><Check aria-hidden="true" />{referencesResolve ? "All references resolve" : "Check references"}</span></div>
  </div>;
}

const questions = [
  ["Does this use customer records?", "You can start with a description of your system and its business rules. The generated records are synthetic, so you do not need to upload real customer records to build a scenario."],
  ["Can I edit the scenario?", "Yes. Review and refine the tables, fields, relationships, workflow paths, and constraints before you generate the dataset."],
  ["What makes the data connected?", "Records are generated with relationships across tables. A visit references a patient, a diagnosis references a visit, and a treatment references a diagnosis, keeping the scenario's context intact."],
  ["How do I check the output?", "Preview the generated tables, inspect quality checks, and review the evaluation report before using your data. Your rules stay visible throughout the workflow."],
  ["What can I export?", "Download generated tables as CSV or JSON. You can also export the evaluation report as JSON or HTML for review."],
  ["Is this demo live generation?", "This page uses a small, curated synthetic patient-care sample to demonstrate linked records. Create a workspace to design your own scenario and run generation."],
];

function MobileNavigation() {
  const menuRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const dismissOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !menuRef.current?.contains(event.target)) {
        menuRef.current?.removeAttribute("open");
      }
    };
    const dismissWithEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && menuRef.current?.open) {
        menuRef.current.open = false;
        menuRef.current.querySelector("summary")?.focus();
      }
    };
    document.addEventListener("pointerdown", dismissOutside);
    document.addEventListener("keydown", dismissWithEscape);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside);
      document.removeEventListener("keydown", dismissWithEscape);
    };
  }, []);

  return <details className="landing-mobile-menu" ref={menuRef}>
    <summary aria-label="Navigation menu"><Menu className="menu-open-icon" aria-hidden="true" /><X className="menu-close-icon" aria-hidden="true" /></summary>
    <nav aria-label="Mobile navigation" onClick={() => menuRef.current?.removeAttribute("open")}>
      <a href="#dataset">Explore the data</a>
      <a href="#workflow">How it works</a>
      <a href="#built-for">Built for complex data</a>
      <Link to="/login">Log in</Link>
      <Link className={buttonVariants()} to="/register">Create workspace <ArrowRight data-icon="inline-end" /></Link>
    </nav>
  </details>;
}

const footerRecords = [
  { label: "Patients", x: 25, y: 20, angle: -12 },
  { label: "Visits", x: 69, y: 34, angle: 9 },
  { label: "Diagnoses", x: 30, y: 54, angle: -7 },
  { label: "Treatments", x: 66, y: 76, angle: 12 },
  { label: "Orders", x: 23, y: 84, angle: 8 },
  { label: "Payments", x: 76, y: 11, angle: -10 },
];
const footerLinks = [[0, 1], [1, 2], [2, 3], [4, 5]];

function CreativeFooter() {
  const [selected, setSelected] = useState(0);
  const [positions, setPositions] = useState(footerRecords.map(({ x, y }) => ({ x, y })));
  const fieldRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ index: number; pointerId: number; x: number; y: number; left: number; top: number } | null>(null);
  const related = footerLinks.flatMap(([from, to]) => from === selected ? [to] : to === selected ? [from] : []);
  const moveRecord = (index: number, x: number, y: number) => setPositions((current) => current.map((position, i) => i === index ? { x: Math.max(19, Math.min(81, x)), y: Math.max(11, Math.min(89, y)) } : position));

  return <footer className="landing-footer">
    <div className="footer-layout">
      <Link className="footer-brand" to="/" aria-label="Syda home"><span className="footer-brand-symbol"><span className="syda-mark" aria-hidden="true"><i /><i /><i /></span></span><span className="footer-brand-name">syda</span></Link>
      <nav className="footer-navigation" aria-label="Footer navigation">
        <div><span>Explore Syda</span><a href="#dataset">See the data</a><a href="#workflow">How it works</a><a href="#built-for">Built for complex data</a></div>
        <div><span>Keep building</span><a href="https://python.syda.ai" target="_blank" rel="noreferrer">Documentation <ArrowRight aria-hidden="true" /></a><a href="https://github.com/syda-ai/syda" target="_blank" rel="noreferrer">GitHub <ArrowRight aria-hidden="true" /></a><Link to="/login">Log in</Link></div>
        <p>Synthetic data.<br />Real-world logic.</p>
        <Link className="footer-start" to="/register">Build your first dataset <span><ArrowRight aria-hidden="true" /></span></Link>
      </nav>
      <div className="footer-playground">
        <div className="footer-playground-heading"><p>Good data has connections.</p><span>Drag a record. Follow a link.</span></div>
        <div className="footer-record-field" ref={fieldRef} role="group" aria-label="Interactive sample record relationships">
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">{footerLinks.map(([from, to]) => <line key={`${from}-${to}`} x1={positions[from].x} y1={positions[from].y} x2={positions[to].x} y2={positions[to].y} data-active={from === selected || to === selected} />)}</svg>
          {footerRecords.map((record, index) => <button key={record.label} className="footer-record" type="button" style={{ left: `${positions[index].x}%`, top: `${positions[index].y}%`, rotate: `${record.angle}deg` }} aria-pressed={selected === index} aria-label={`${record.label}: connected to ${footerLinks.flatMap(([from, to]) => from === index ? [footerRecords[to].label] : to === index ? [footerRecords[from].label] : []).join(", ")}. Drag or use arrow keys to move.`} data-connected={related.includes(index)} onClick={() => setSelected(index)} onPointerDown={(event) => {
            if (event.button !== 0) return;
            setSelected(index);
            dragRef.current = { index, pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: positions[index].x, top: positions[index].y };
            event.currentTarget.setPointerCapture(event.pointerId);
          }} onPointerMove={(event) => {
            const drag = dragRef.current, field = fieldRef.current;
            if (!drag || drag.index !== index || drag.pointerId !== event.pointerId || !field) return;
            moveRecord(index, drag.left + (event.clientX - drag.x) / field.clientWidth * 100, drag.top + (event.clientY - drag.y) / field.clientHeight * 100);
          }} onPointerUp={() => { dragRef.current = null; }} onPointerCancel={() => { dragRef.current = null; }} onLostPointerCapture={() => { dragRef.current = null; }} onKeyDown={(event) => {
            const offsets: Record<string, [number, number]> = { ArrowLeft: [-3, 0], ArrowRight: [3, 0], ArrowUp: [0, -3], ArrowDown: [0, 3] };
            const offset = offsets[event.key];
            if (offset) { event.preventDefault(); setSelected(index); moveRecord(index, positions[index].x + offset[0], positions[index].y + offset[1]); }
          }}><span className="footer-record-dot" aria-hidden="true" />{record.label}</button>)}
        </div>
        <div className="footer-relationship" aria-live="polite" aria-atomic="true"><span>{footerRecords[selected].label}</span><ArrowRight aria-hidden="true" /><span>{related.map((index) => footerRecords[index].label).join(" + ")}</span></div>
      </div>
      <div className="footer-bottom"><span>© {new Date().getFullYear()} Syda</span><a href="https://github.com/syda-ai/syda/blob/main/LICENSE" target="_blank" rel="noreferrer">Open source license <ArrowRight aria-hidden="true" /></a></div>
    </div>
  </footer>;
}

export default function Landing() {
  const pageRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver((entries) => entries.forEach((entry) => {
      if (entry.isIntersecting) { entry.target.classList.add("is-visible"); observer.unobserve(entry.target); }
    }), { threshold: 0.12 });
    pageRef.current?.querySelectorAll("[data-reveal], [data-illustration]").forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, []);

  return <main className="syda-landing" ref={pageRef}>
    <a className="landing-skip" href="#main-content">Skip to content</a>
    <header className="landing-header">
      <Link className="landing-wordmark" to="/" aria-label="Syda home"><span className="syda-mark" aria-hidden="true"><i /><i /><i /></span>syda</Link>
      <nav aria-label="Main navigation"><a href="#dataset">Explore the data</a><a href="#workflow">How it works</a><a href="#built-for">Built for complex data</a></nav>
      <div className="landing-account"><ThemeToggle /><Link to="/login">Log in</Link><Link className={buttonVariants()} to="/register">Create workspace <ArrowRight data-icon="inline-end" /></Link><MobileNavigation /></div>
    </header>

    <section className="landing-hero landing-frame" id="main-content" aria-labelledby="hero-title">
      <picture className="hero-art">
        <source media="(max-width: 1100px)" srcSet="/images/syda-connected-records.webp" />
        <img src="/images/syda-connected-records-wide.webp" width="1774" height="887" fetchPriority="high" alt="Paper records for a patient, visit, diagnosis, and treatment connected by one green thread, with each record referencing the one before it." />
      </picture>
      <div className="hero-copy">
        <h1 id="hero-title"><span><span>Synthetic data.</span></span><span><span>Real-world logic.</span></span></h1>
        <p>Describe the world your data lives in. Syda generates connected records that follow its rules, without using customer data.</p>
        <div className="hero-actions"><Link className={buttonVariants({ size: "lg" })} to="/register">Build your first dataset <ArrowRight data-icon="inline-end" /></Link><a className="landing-text-link" href="#dataset">See it in action <ArrowRight aria-hidden="true" /></a></div>
      </div>
    </section>

    <section className="landing-demo landing-frame" id="dataset" aria-labelledby="dataset-title">
      <div className="dataset-copy" data-reveal>
        <h2 id="dataset-title">Follow the connections.</h2>
        <p>A patient has visits. A visit has a diagnosis. A diagnosis has a treatment. The meaning lives in how they connect.</p>
        <p>Select a record to inspect its fields and follow its links. Switch patients to explore a different care path.</p>
      </div>
      <DatasetDemo />
    </section>

    <section className="landing-workflow landing-frame" id="workflow" aria-labelledby="workflow-title">
      <div className="section-intro" data-reveal><h2 id="workflow-title">From brief<br />to dataset.</h2><p>Real systems have relationships, decisions, and exceptions. Your test data should, too.</p></div>
      <div className="workflow-exchanges" id="built-for">
        <article className="workflow-exchange">
          <div className="exchange-copy" data-reveal><h3>Describe the system.</h3><p>Start with the entities, lifecycle, and edge cases. Syda turns your brief into an editable scenario.</p></div>
          <div className="brief-fragment" data-ui data-reveal><div className="fragment-heading"><span>Your scenario</span><GitBranch aria-hidden="true" /></div><p>“Create patient care data with visits, diagnoses, and treatments. Include routine follow-ups and escalated care.”</p><div className="schema-response"><span>Patient</span><span>Visit</span><span>Diagnosis</span><span>Treatment</span></div><span className="fragment-caption">Four entities, linked by their IDs.</span></div>
        </article>
        <article className="workflow-exchange">
          <div className="exchange-copy" data-reveal><h3>Keep the logic.</h3><p>Refine schemas, paths, and constraints before generating. Define how records behave together, beyond individual fields.</p></div>
          <div className="rules-fragment" data-ui data-reveal><div className="fragment-heading"><span>Scenario constraints</span><span>Patient care</span></div><ul><li><span>Every visit belongs to a patient</span><span>Relationship</span></li><li><span>Follow-ups happen after the first visit</span><span>Sequence</span></li><li><span>Escalated care includes a specialist visit</span><span>Care path</span></li></ul></div>
        </article>
        <article className="workflow-exchange">
          <div className="exchange-copy" data-reveal><h3>Inspect. Then export.</h3><p>Preview your tables and review quality checks. Export CSV or JSON when the data is ready for your tests.</p></div>
          <div className="export-fragment" data-ui data-reveal><div className="fragment-heading"><span>Sample relationship checks</span><span>{checks.filter((check) => check.pass).length} / {checks.length} passed</span></div><ul>{checks.map((check) => <li key={check.label}><span>{check.label}</span><span><Check aria-hidden="true" />{check.pass ? "Passed" : "Failed"}</span></li>)}</ul></div>
        </article>
      </div>
    </section>

    <section className="landing-faq landing-frame" aria-labelledby="faq-title"><h2 id="faq-title" data-reveal>Before <br />you build.</h2><div className="faq-list">{questions.map(([question, answer]) => <details name="landing-faq" key={question} data-reveal><summary>{question}<Plus aria-hidden="true" /></summary><p>{answer}</p></details>)}</div></section>

    <CreativeFooter />
  </main>;
}
