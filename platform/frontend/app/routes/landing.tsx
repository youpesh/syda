import { Link } from "react-router";
import { HugeiconsIcon } from "@hugeicons/react";
import { AiMagicIcon, ArrowRight02Icon, CheckmarkCircle02Icon } from "@hugeicons/core-free-icons";

import type { Route } from "./+types/landing";
import { Badge } from "~/components/ui/badge";
import { buttonVariants } from "~/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "~/components/ui/card";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Syda — Connected synthetic test data" },
    { name: "description", content: "Describe your data and business rules. Syda generates linked synthetic test datasets and checks that their relationships hold." },
  ];
}

const steps = [
  { number: "01", title: "Describe the system", copy: "Start with your entities, lifecycle, rules, and edge cases. Syda turns the brief into an editable scenario." },
  { number: "02", title: "Shape the data", copy: "Refine schemas, relationships, paths, and constraints before a single record is generated." },
  { number: "03", title: "Generate and inspect", copy: "Create linked datasets, review quality checks, and export the results for your team." },
];

export default function Landing() {
  return <main className="min-h-svh bg-background text-foreground">
    <header className="mx-auto flex w-full max-w-7xl items-center justify-between gap-6 px-5 py-5 sm:px-8">
      <Link className="flex items-center gap-2.5" to="/" aria-label="Syda home">
        <span className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground"><HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} /></span>
        <span className="text-lg font-semibold tracking-tight">Syda</span>
      </Link>
      <nav className="hidden items-center gap-7 text-sm text-muted-foreground md:flex" aria-label="Main navigation">
        <a className="transition-colors hover:text-foreground" href="#workflow">How it works</a>
        <a className="transition-colors hover:text-foreground" href="#built-for">Built for complex data</a>
      </nav>
      <div className="flex items-center gap-2">
        <Link className={buttonVariants({ variant: "ghost" })} to="/login">Log in</Link>
        <Link className={buttonVariants({})} to="/register">Get started <HugeiconsIcon data-icon="inline-end" icon={ArrowRight02Icon} strokeWidth={2} /></Link>
      </div>
    </header>

    <section className="mx-auto grid w-full max-w-7xl items-center gap-12 px-5 pb-20 pt-10 sm:px-8 lg:grid-cols-[0.9fr_1.1fr] lg:gap-16 lg:pb-28 lg:pt-16">
      <div className="flex flex-col items-start gap-6">
        <Badge variant="secondary" className="rounded-full px-3 py-1">Synthetic test data for connected systems</Badge>
        <h1 className="max-w-2xl text-5xl leading-[1.04] font-semibold tracking-[-0.055em] sm:text-6xl lg:text-7xl">Generate test data that <span className="text-primary">stays connected.</span></h1>
        <p className="max-w-xl text-base leading-7 text-muted-foreground sm:text-lg">Describe your data and business rules. Syda creates linked synthetic datasets, checks that the relationships hold, and lets you export the results—without using real customer records.</p>
        <div className="flex flex-wrap items-center gap-3">
          <Link className={buttonVariants({ size: "lg" })} to="/register">Create your workspace <HugeiconsIcon data-icon="inline-end" icon={ArrowRight02Icon} strokeWidth={2} /></Link>
          <a className={buttonVariants({ variant: "outline", size: "lg" })} href="#workflow">See how it works</a>
        </div>
        <p className="text-xs text-muted-foreground">Start with a description. Review the data before you use it.</p>
      </div>

      <div className="relative mx-auto w-full max-w-2xl">
        <div className="absolute -inset-5 -z-10 rounded-[2rem] bg-primary/10 blur-2xl" />
        <Card className="overflow-hidden border-border/80 bg-card shadow-xl shadow-primary/5">
          <CardHeader className="flex flex-row items-start justify-between gap-4 border-b bg-muted/20 px-5 py-4">
            <div className="flex flex-col gap-1"><p className="text-xs text-muted-foreground">Example output</p><CardTitle className="text-base">Patient care data</CardTitle></div>
            <Badge className="rounded-full" variant="secondary">Illustrative example</Badge>
          </CardHeader>
          <CardContent className="flex flex-col gap-5 p-5 sm:p-6">
            <div className="grid grid-cols-4 gap-2 sm:gap-3">
              {["Patient", "Visit", "Diagnosis", "Treatment"].map((step, index) => <div className="relative flex min-h-20 flex-col justify-between rounded-2xl border bg-background p-2.5 sm:p-3" key={step}>
                <span className="text-[10px] text-muted-foreground">0{index + 1}</span><span className="text-xs font-medium sm:text-sm">{step}</span>
                {index < 3 && <span className="absolute top-1/2 -right-2 z-10 hidden h-px w-2 bg-border sm:block" />}
              </div>)}
            </div>
            <div className="flex flex-wrap gap-2">
              <Badge variant="outline">5,000 patients</Badge><Badge variant="outline">4 connected tables</Badge><Badge variant="outline">3 care paths</Badge>
            </div>
            <div className="overflow-hidden rounded-2xl border">
              <div className="flex items-center justify-between border-b bg-muted/30 px-3 py-2.5"><span className="text-xs font-medium">Sample rows</span><span className="text-[10px] text-muted-foreground">Patient and visit IDs stay linked</span></div>
              <div className="grid grid-cols-[1fr_1fr_1.1fr] gap-2 px-3 py-2 text-[10px] font-medium text-muted-foreground"><span>patient_id</span><span>visit_id</span><span>care_path</span></div>
              {[ ["PT-00421", "VS-01934", "Routine follow-up"], ["PT-00422", "VS-01935", "Escalated care"], ["PT-00423", "VS-01936", "Routine follow-up"] ].map((row) => <div className="grid grid-cols-[1fr_1fr_1.1fr] gap-2 border-t px-3 py-2 text-[10px] sm:text-xs" key={row[0]}>{row.map((cell) => <span className="truncate" key={cell}>{cell}</span>)}</div>)}
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground"><HugeiconsIcon icon={CheckmarkCircle02Icon} strokeWidth={2} /><span>Linked records, ready to review and export.</span></div>
          </CardContent>
        </Card>
      </div>
    </section>

    <section className="border-y bg-muted/20" id="built-for">
      <div className="mx-auto grid max-w-7xl gap-3 px-5 py-7 sm:grid-cols-3 sm:px-8">
        {[ ["Connected", "Relationships stay intact across every generated table."], ["Controlled", "Define the paths, proportions, and business rules yourself."], ["Inspectable", "Review previews and quality checks before using your data."] ].map(([title, copy]) => <div className="flex flex-col gap-1 px-1 py-2 sm:px-4" key={title}><p className="text-sm font-medium">{title}</p><p className="text-xs leading-5 text-muted-foreground">{copy}</p></div>)}
      </div>
    </section>

    <section className="mx-auto flex w-full max-w-7xl flex-col gap-10 px-5 py-20 sm:px-8 lg:py-28" id="workflow">
      <div className="max-w-2xl"><p className="text-sm font-medium text-primary">A clear path from brief to dataset</p><h2 className="mt-3 text-3xl font-semibold tracking-[-0.04em] sm:text-4xl">Keep the meaning in your data.</h2><p className="mt-3 text-sm leading-6 text-muted-foreground sm:text-base">Syda keeps the logic visible, so generated records make sense together—not just one row at a time.</p></div>
      <div className="grid gap-4 md:grid-cols-3">{steps.map((step) => <Card className="bg-background" key={step.number}><CardContent className="flex flex-col gap-7 p-5 sm:p-6"><span className="text-sm font-medium text-primary">{step.number}</span><div className="flex flex-col gap-2"><h3 className="text-lg font-semibold tracking-tight">{step.title}</h3><p className="text-sm leading-6 text-muted-foreground">{step.copy}</p></div></CardContent></Card>)}</div>
    </section>

    <section className="px-5 pb-20 sm:px-8 lg:pb-28">
      <div className="mx-auto flex max-w-7xl flex-col items-start justify-between gap-6 rounded-4xl bg-primary px-6 py-8 text-primary-foreground sm:flex-row sm:items-center sm:px-10 sm:py-10">
        <div className="flex flex-col gap-2"><h2 className="text-2xl font-semibold tracking-tight">Build data you can trust to behave.</h2><p className="text-sm text-primary-foreground/75">Start designing your first synthetic scenario.</p></div>
        <Link className={buttonVariants({ variant: "secondary", size: "lg" })} to="/register">Get started <HugeiconsIcon data-icon="inline-end" icon={ArrowRight02Icon} strokeWidth={2} /></Link>
      </div>
    </section>

    <footer className="border-t"><div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-5 py-6 text-xs text-muted-foreground sm:px-8"><span>© {new Date().getFullYear()} Syda</span><span>Synthetic data, built around real workflows.</span></div></footer>
  </main>;
}
