import { Link } from "react-router";
import { ConnectionPicker } from "~/components/database-workspace";
import {
  Fragment,
  useEffect,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  Add01Icon,
  AiMagicIcon,
  Analytics01Icon,
  ArrowUp02Icon,
  Attachment01Icon,
  ChartEvaluationIcon,
  Database02Icon,
  MoreHorizontalIcon,
  SecurityCheckIcon,
  SparklesIcon,
  UserCircleIcon,
} from "@hugeicons/core-free-icons";

import type { Route } from "./+types/ui-preview";
import { Button } from "~/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "~/components/ui/card";
import {
  Bubble,
  BubbleContent,
} from "~/components/ui/bubble";
import {
  Message,
  MessageAvatar,
  MessageContent,
} from "~/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "~/components/ui/message-scroller";
import { Separator } from "~/components/ui/separator";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "~/components/ui/sidebar";
import { Textarea } from "~/components/ui/textarea";
import { ReviewWorkspace, useReviewWorkspace, viewTitles } from "~/components/review-workspace";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Syda — UI prototype preview" },
    {
      name: "description",
      content: "Design, generate, and evaluate trustworthy synthetic data.",
    },
  ];
}

const recentScenarios = [
  { name: "Insurance claims", subtitle: "10k records · 15% denied" },
  { name: "Patient journey", subtitle: "Last edited yesterday" },
  { name: "E-commerce orders", subtitle: "25k records · 4 tables" },
];

const workspaceItems = [
  { name: "Scenario", icon: AiMagicIcon, view: "chat" },
  { name: "Connections", icon: Database02Icon, view: "connections" },
  { name: "Schema review", icon: SecurityCheckIcon, view: "schema" },
  { name: "Run history", icon: ChartEvaluationIcon, view: "runs" },
] as const;

const promptSuggestions = [
  {label:"Oncology cohort",prompt:"Create an oncology cohort of 100 patients with lung and colorectal cancers, ages 40–80, with linked diagnoses, claims, and payments. Diagnosis precedes claim; denied claims have no payment."},
  {
    label: "Insurance claims",
    prompt:
      "Generate 10,000 insurance claims where 15% are denied, with realistic diagnoses, providers, and payment timelines.",
  },
  {
    label: "Patient journeys",
    prompt:
      "Create 5,000 longitudinal patient journeys with diagnoses, treatments, follow-ups, and realistic care gaps.",
  },
  {
    label: "E-commerce orders",
    prompt:
      "Build an e-commerce dataset with 25,000 orders, customers, products, refunds, and seasonal purchasing patterns.",
  },
];

type GenerationStatus =
  | "idle"
  | "generating"
  | "validating"
  | "evaluating"
  | "complete";

type ScenarioConfiguration = {
  title: string;
  description: string;
  recordCount: number;
  secondaryMetric: { label: string; value: string };
  workflow: string[];
  rules: string[];
};

type ConversationTurn = {
  id: string;
  prompt: string;
  response: string;
  scenario: ScenarioConfiguration;
  status: GenerationStatus;
};

const scenarioProfiles: Array<{
  matches: RegExp;
  scenario: ScenarioConfiguration;
}> = [
  {
    matches: /insurance|claim|denial|oncolog|cancer/i,
    scenario: {
      title: "Insurance claims",
      description: "Healthcare claims with causal and referential constraints.",
      recordCount: 10_000,
      secondaryMetric: { label: "Denial rate", value: "15%" },
      workflow: ["Patient", "Diagnosis", "Claim", "Adjudication", "Payment"],
      rules: [
        "Diagnosis occurs before a claim",
        "Adjudication occurs before payment",
        "Denied claims cannot be paid",
      ],
    },
  },
  {
    matches: /patient|journey|care gap/i,
    scenario: {
      title: "Patient journeys",
      description: "Longitudinal care journeys with clinically ordered events.",
      recordCount: 5_000,
      secondaryMetric: { label: "Care-gap rate", value: "8%" },
      workflow: ["Patient", "Visit", "Diagnosis", "Treatment", "Follow-up"],
      rules: [
        "Visits occur before diagnoses",
        "Treatments require a diagnosis",
        "Follow-ups occur after treatment",
      ],
    },
  },
  {
    matches: /e-commerce|commerce|orders?|refund|product/i,
    scenario: {
      title: "E-commerce orders",
      description: "Relational commerce data with orders, products, and refunds.",
      recordCount: 25_000,
      secondaryMetric: { label: "Tables", value: "4" },
      workflow: ["Customer", "Cart", "Order", "Payment", "Fulfillment"],
      rules: [
        "Orders contain valid products",
        "Payments occur after checkout",
        "Refunds cannot exceed payments",
      ],
    },
  },
];

const defaultScenario: ScenarioConfiguration = {
  title: "Custom scenario",
  description: "A structured synthetic-data scenario based on your requirements.",
  recordCount: 10_000,
  secondaryMetric: { label: "Rules", value: "3" },
  workflow: ["Source", "Event", "Decision", "Outcome"],
  rules: [
    "Events preserve their declared order",
    "Relationships maintain referential integrity",
    "Generated records satisfy scenario constraints",
  ],
};

function cloneScenario(scenario: ScenarioConfiguration): ScenarioConfiguration {
  return {
    ...scenario,
    secondaryMetric: { ...scenario.secondaryMetric },
    workflow: [...scenario.workflow],
    rules: [...scenario.rules],
  };
}

function scenarioFromPrompt(
  prompt: string,
  current?: ScenarioConfiguration,
): ScenarioConfiguration {
  const selected = current
    ? cloneScenario(current)
    : cloneScenario(
        scenarioProfiles.find((profile) => profile.matches.test(prompt))?.scenario ??
          defaultScenario,
      );

  const recordMatch = prompt.match(
    /([\d,]+)\s+(?:records?|insurance claims?|claims?|patient journeys?|patients?|orders?)/i,
  );
  if (recordMatch) {
    selected.recordCount = Number(recordMatch[1].replaceAll(",", ""));
  }

  if (/oncolog|cancer/i.test(prompt)) {
    selected.title = "Oncology cohort";
    selected.description = "Preview a linked healthcare scenario before generation.";
    selected.secondaryMetric = { label: "Related tables", value: "4" };
    selected.workflow = ["Patient", "Diagnosis", "Claim", "Payment"];
    selected.rules = ["Diagnosis precedes claim", "Denied claims have no payment", "Review demographic and cancer-type requirements"];
  }

  const denialMatch =
    prompt.match(/([\d.]+)%\s+(?:are\s+)?denied/i) ??
    prompt.match(/denial(?:s| rate)?(?:\s+(?:to|of|at))?\s*([\d.]+)%/i);
  if (denialMatch && selected.title === "Insurance claims") {
    selected.secondaryMetric = {
      label: "Denial rate",
      value: `${denialMatch[1]}%`,
    };
  }

  return selected;
}

function createTurn(
  prompt: string,
  current?: ScenarioConfiguration,
): ConversationTurn {
  return {
    id: crypto.randomUUID(),
    prompt,
    response: current
      ? "I’ve updated the structured configuration. The previous version remains in the conversation for comparison."
      : "I’ve created a structured scenario from your requirements. Review the workflow and rules before generating.",
    scenario: scenarioFromPrompt(prompt, current),
    status: "idle",
  };
}

function PromptComposer({
  value,
  onChange,
  onSubmit,
  compact = false,
  connector,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  compact?: boolean;
  connector: React.ReactNode;
}) {
  const [attachment,setAttachment] = useState("");
  const [attachmentError,setAttachmentError] = useState("");
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  };

  return (
    <form
      className="w-full"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="overflow-hidden rounded-3xl border bg-card shadow-[0_14px_44px_-28px_oklch(0.2_0.02_260/0.45)] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring/30">
        <Textarea
          aria-label="Describe your synthetic data scenario"
          autoFocus={!compact}
          className={compact ? "min-h-20 resize-none rounded-none border-0 shadow-none focus-visible:ring-0" : "min-h-28 resize-none rounded-none border-0 shadow-none focus-visible:ring-0"}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Describe the data you want to generate…"
          value={value}
        />
        <div className="flex flex-wrap items-center gap-2 border-t p-3">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            <label className="inline-flex h-9 shrink-0 cursor-pointer items-center justify-center gap-2 rounded-full border px-3 text-sm font-medium focus-within:ring-2 focus-within:ring-ring [&_svg]:size-4" title="Add a sample or schema"><HugeiconsIcon icon={Attachment01Icon} strokeWidth={2}/><span>Attach</span><input className="sr-only" aria-label="Attach sample or schema" type="file" accept=".csv,.json,.txt" onChange={async e=>{const file=e.target.files?.[0];e.target.value="";if(!file)return;setAttachmentError("");if(file.size>100000||! /\.(csv|json|txt)$/i.test(file.name)){setAttachmentError("Choose a CSV, JSON, or TXT file under 100 KB.");return;}try{const text=await file.text();onChange(value+`\n\nAttached context (${file.name}):\n${text}`);setAttachment(file.name);}catch{setAttachmentError("Could not read this file. Please try again.");}}}/></label>
            {connector}
          </div>
          <Button className="ml-auto shrink-0 self-end" aria-label="Send prompt" disabled={!value.trim()} size="icon" type="submit">
            <HugeiconsIcon icon={ArrowUp02Icon} strokeWidth={2} />
          </Button>
        </div>
      </div>
      {attachment&&<p role="status" className="mt-2 text-xs text-muted-foreground">Added {attachment} to the prompt. Context is local; the preview does not parse it into a schema.</p>}
      {attachmentError&&<p role="alert" className="mt-2 text-xs text-destructive">{attachmentError}</p>}
      {!compact && (
        <p className="mt-3 text-center text-xs text-muted-foreground">
          Syda can make mistakes. Review rules and evaluations before using generated data.
        </p>
      )}
    </form>
  );
}

function ScenarioCard({
  scenario,
  status,
  onGenerate,
}: {
  scenario: ScenarioConfiguration;
  status: GenerationStatus;
  onGenerate: () => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>{scenario.title}</CardTitle>
          <CardDescription>{scenario.description}</CardDescription>
          <CardAction>
            <Button aria-label="Scenario actions" size="icon-sm" variant="ghost">
              <HugeiconsIcon icon={MoreHorizontalIcon} strokeWidth={2} />
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <dl className="grid grid-cols-2 gap-x-8 gap-y-2 border-y py-4 tabular-nums">
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">Records</dt>
              <dd className="font-medium">{scenario.recordCount.toLocaleString()}</dd>
            </div>
            <div className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">{scenario.secondaryMetric.label}</dt>
              <dd className="font-medium">{scenario.secondaryMetric.value}</dd>
            </div>
          </dl>

          <section className="flex flex-col gap-2" aria-labelledby="workflow-title">
            <h3 id="workflow-title" className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Workflow
            </h3>
            <div className="flex flex-wrap items-center gap-1.5 text-xs font-medium">
              {scenario.workflow.map((step, index) => (
                <span className="flex items-center gap-1.5" key={step}>
                  <span>{step}</span>
                  {index < scenario.workflow.length - 1 && (
                    <span className="text-muted-foreground">→</span>
                  )}
                </span>
              ))}
            </div>
          </section>

          <section className="flex flex-col gap-2" aria-labelledby="rules-title">
            <h3 id="rules-title" className="text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Rules
            </h3>
            <ul className="flex flex-col gap-1.5">
              {scenario.rules.map((rule) => (
                <li className="flex items-center gap-2" key={rule}>
                  <span className="flex size-4 items-center justify-center bg-primary text-[10px] text-primary-foreground">✓</span>
                  {rule}
                </li>
              ))}
            </ul>
          </section>
        </CardContent>
        <CardFooter className="flex-wrap justify-end gap-2">
          <Button onClick={onGenerate} type="button" variant="outline">Edit configuration</Button>
          <Button disabled={status !== "idle"} onClick={onGenerate} type="button">
            <HugeiconsIcon data-icon="inline-start" icon={SparklesIcon} strokeWidth={2} />
            {status === "idle"
              ? "Review & generate"
              : status === "complete"
                ? "Generated"
                : `${status[0].toUpperCase()}${status.slice(1)}`}
          </Button>
        </CardFooter>
      </Card>


    </div>
  );
}

export default function Home() {
  const review = useReviewWorkspace();
  const [prompt, setPrompt] = useState("");
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [history,setHistory] = useState<{id:string;title:string;prompts:string[]}[]>([]);
  const [conversationId,setConversationId] = useState("");
  const [historyReady,setHistoryReady] = useState(false);
  useEffect(()=>{try{const saved=JSON.parse(localStorage.getItem("syda-ui-preview-chat-history-v1")??"[]");if(Array.isArray(saved))setHistory(saved.filter(c=>c&&typeof c.id==="string"&&typeof c.title==="string"&&Array.isArray(c.prompts)&&c.prompts.every((p:unknown)=>typeof p==="string")));}catch{}setHistoryReady(true);},[]);
  useEffect(()=>{if(historyReady)try{localStorage.setItem("syda-ui-preview-chat-history-v1",JSON.stringify(history));}catch{}},[history,historyReady]);
  const connector=<div className="contents"><ConnectionPicker optional db={review.db} onChange={()=>{review.setConnected(false);review.setReviewed(false);review.setConnectionError("");}}/><Button aria-label="Manage database connections" type="button" variant="outline" className="h-9" onClick={()=>review.setView("connections")}><HugeiconsIcon icon={Database02Icon} strokeWidth={2}/>Connections</Button></div>;
  const latestScenario = turns.at(-1)?.scenario;

  const submitPrompt = () => {
    if (!prompt.trim()) return;
    const nextTurn = createTurn(prompt.trim(), latestScenario);
    setTurns((current) => [
      ...current.map((turn) =>
        turn.status !== "complete" ? { ...turn, status: "idle" as const } : turn,
      ),
      nextTurn,
    ]);
    const id=conversationId||crypto.randomUUID();
    setConversationId(id);
    const item={id,title:nextTurn.scenario.title,prompts:[...turns.map(t=>t.prompt),prompt.trim()]};
    setHistory(h=>[item,...h.filter(c=>c.id!==id)]);
    setPrompt("");
  };

  const startNewScenario = () => {
    review.setView("chat");
    setPrompt("");
    setTurns([]);
    setConversationId("");
  };

  const openExample = (name: string) => {
    setConversationId("");
    review.setView("chat");
    const example = promptSuggestions.find((item) =>
      name.toLowerCase().startsWith(item.label.split(" ")[0].toLowerCase()),
    );
    setTurns([createTurn(example?.prompt ?? promptSuggestions[0].prompt)]);
    setPrompt("");
  };

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader className="gap-3 p-3">
          <div className="flex h-8 items-center gap-2 px-1">
            <div className="flex size-6 items-center justify-center bg-primary text-primary-foreground">
              <HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} />
            </div>
            <span className="text-sm font-semibold tracking-[-0.02em] group-data-[collapsible=icon]:hidden">Syda</span>
          </div>
          <Button className="w-full group-data-[collapsible=icon]:px-0" onClick={startNewScenario} variant="outline">
            <HugeiconsIcon data-icon="inline-start" icon={Add01Icon} strokeWidth={2} />
            <span className="group-data-[collapsible=icon]:hidden">New scenario</span>
          </Button>
        </SidebarHeader>

        <SidebarContent>
          <SidebarGroup><SidebarGroupLabel>Conversation history</SidebarGroupLabel><SidebarGroupContent><SidebarMenu>{history.map(c=><SidebarMenuItem key={c.id}><SidebarMenuButton isActive={conversationId===c.id} onClick={()=>{let previous:ScenarioConfiguration|undefined;const restored=c.prompts.map(p=>{const turn=createTurn(p,previous);previous=turn.scenario;return turn;});setTurns(restored);setConversationId(c.id);setPrompt("");review.setView("chat");}} tooltip={c.title}><span className="truncate">{c.title} · {c.prompts.length} messages</span></SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu>{!history.length&&<p className="px-2 text-xs text-muted-foreground">Your conversations will appear here.</p>}<p className="px-2 py-2 text-[10px] text-muted-foreground">Saved in this browser. Avoid sensitive data in prompts.</p></SidebarGroupContent></SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>Example scenarios</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {recentScenarios.map((scenario) => (
                  <SidebarMenuItem key={scenario.name}>
                    <SidebarMenuButton isActive={latestScenario?.title.toLowerCase().startsWith(scenario.name.toLowerCase().split(" ")[0])} onClick={() => openExample(scenario.name)} tooltip={scenario.name}>
                      <HugeiconsIcon icon={Analytics01Icon} strokeWidth={2} />
                      <span className="flex min-w-0 flex-col group-data-[collapsible=icon]:hidden">
                        <span className="truncate">{scenario.name}</span>
                        <span className="truncate text-[10px] font-normal text-muted-foreground">{scenario.subtitle}</span>
                      </span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>

          <SidebarGroup>
            <SidebarGroupLabel>Workspace</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {workspaceItems.map((item) => (
                  <SidebarMenuItem key={item.name}>
                    <SidebarMenuButton isActive={review.view === item.view} onClick={() => review.setView(item.view)} tooltip={item.name}>
                      <HugeiconsIcon icon={item.icon} strokeWidth={2} />
                      <span>{item.name}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton render={<Link to="/login" />} tooltip="Sign in">
                <HugeiconsIcon icon={UserCircleIcon} strokeWidth={2} />
                <span>Sign in</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset className="h-svh overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b px-4">
          <div className="flex items-center gap-2">
            <SidebarTrigger />
            <Separator className="h-4" orientation="vertical" />
            <div>
              <p className="text-xs font-medium">{review.view === "chat" ? latestScenario?.title ?? "New scenario" : viewTitles[review.view]}</p>
              <p className="hidden text-[10px] text-muted-foreground sm:block">
                {review.view === "chat" ? turns.length ? "Draft configuration" : "Describe what you want to generate" : "Capstone workspace"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2"><Link className="text-xs underline" to="/app">Back to live studio</Link>
          </div>
        </header>

        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b bg-muted/40 px-4 py-2 text-[10px] text-muted-foreground"><span>UI prototype · sample data only. This page does not use live accounts, conversations, or generation.</span><Button size="sm" variant="ghost" onClick={() => review.setView(review.view === "chat" ? "connections" : "chat")}>{review.view === "chat" ? "Connect a database" : "Back to scenario"}</Button></div>
        {review.view !== "chat" ? <ReviewWorkspace review={review} /> : !turns.length ? (
          <main className="relative flex min-h-0 flex-1 items-center justify-center overflow-y-auto px-5 py-12">
            <div aria-hidden="true" className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_38%,oklch(0.96_0.025_265),transparent_38%)]" />
            <div className="relative flex w-full max-w-2xl flex-col items-center">
              <div className="mb-5 flex size-10 items-center justify-center border bg-card shadow-sm">
                <HugeiconsIcon icon={SparklesIcon} strokeWidth={1.8} />
              </div>
              <p className="mb-2 text-xs font-medium text-muted-foreground">New synthetic data scenario</p>
              <h1 className="max-w-lg text-center text-3xl font-semibold tracking-[-0.035em] sm:text-4xl">What should we generate?</h1>
              <p className="mb-8 mt-3 max-w-md text-center text-sm leading-6 text-muted-foreground">
                Describe the entities, relationships, constraints, and edge cases. Syda will turn them into a testable scenario.
              </p>
              <PromptComposer connector={connector} onChange={setPrompt} onSubmit={submitPrompt} value={prompt} />
              <div className="mt-5 flex flex-wrap justify-center gap-2">
                {promptSuggestions.map((suggestion) => (
                  <Button key={suggestion.label} onClick={() => setPrompt(suggestion.prompt)} size="sm" type="button" variant="outline">
                    {suggestion.label}
                  </Button>
                ))}
              </div>
            </div>
          </main>
        ) : (
          <main className="flex min-h-0 flex-1 flex-col">
            <MessageScrollerProvider autoScroll>
              <MessageScroller className="flex-1">
                <MessageScrollerViewport>
                  <MessageScrollerContent className="mx-auto w-full max-w-3xl px-5 py-8 sm:px-8 sm:py-10">
                    {turns.map((turn) => (
                      <Fragment key={turn.id}>
                        <MessageScrollerItem
                          messageId={`${turn.id}-user`}
                          scrollAnchor
                        >
                          <Message align="end">
                            <MessageContent>
                              <Bubble variant="secondary">
                                <BubbleContent>{turn.prompt}</BubbleContent>
                              </Bubble>
                            </MessageContent>
                          </Message>
                        </MessageScrollerItem>
                        <MessageScrollerItem messageId={`${turn.id}-assistant`}>
                          <Message>
                            <MessageAvatar className="self-start bg-primary text-primary-foreground">
                              <HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} />
                            </MessageAvatar>
                            <MessageContent>
                              <Bubble className="w-full" variant="ghost">
                                <BubbleContent className="w-full">
                                  <p className="mb-3 text-xs leading-5">{turn.response}</p>
                                  <ScenarioCard
                                    onGenerate={() => review.configure(turn.scenario.title, turn.scenario.recordCount, turn.prompt)}
                                    scenario={turn.scenario}
                                    status={turn.status}
                                  />
                                </BubbleContent>
                              </Bubble>
                            </MessageContent>
                          </Message>
                        </MessageScrollerItem>
                      </Fragment>
                    ))}
                  </MessageScrollerContent>
                </MessageScrollerViewport>
                <MessageScrollerButton />
              </MessageScroller>
            </MessageScrollerProvider>
            <div className="shrink-0 border-t bg-background px-5 py-4">
              <div className="mx-auto w-full max-w-3xl">
                <PromptComposer connector={connector} compact onChange={setPrompt} onSubmit={submitPrompt} value={prompt} />
              </div>
            </div>
          </main>
        )}
        {review.notice && <div role="status" className="fixed right-4 bottom-4 z-50 flex max-w-[calc(100vw-2rem)] items-center gap-3 border bg-card p-4 text-xs shadow-lg"><span>{review.notice}</span><Button size="sm" variant="ghost" onClick={() => review.setNotice("")}>Dismiss</Button></div>}
      </SidebarInset>
    </SidebarProvider>
  );
}
