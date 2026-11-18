import type { SchemaSource } from "~/components/studio/live-connections";
import { SchemaExplorer } from "~/components/studio/schema-explorer";
import { RunQualitySummary } from "~/components/studio/run-quality-summary";
import { createChatFetch } from "~/lib/chat-fetch";
import { saveScenario } from "~/lib/save-scenario";
import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { useChat } from "@ai-sdk/react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  ArrowUp02Icon,
  SparklesIcon,
} from "@hugeicons/core-free-icons";

import type { Route } from "./+types/home";
import { Button, buttonVariants } from "~/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "~/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "~/components/ui/alert";
import { Badge } from "~/components/ui/badge";
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
import { Spinner } from "~/components/ui/spinner";
import { Textarea } from "~/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "~/components/ui/dialog";
import { Progress, ProgressLabel, ProgressValue } from "~/components/ui/progress";
import { AppShell } from "~/components/studio/app-shell";
import { CleanMessageText } from "~/components/studio/clean-message-text";
import { DataPreview } from "~/components/studio/data-preview";
import type { ChatRunReference, CostEstimate, GenerationJob, SavedConversation, ScenarioConfiguration } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Syda — Synthetic data, shaped by you" },
    {
      name: "description",
      content: "Design, generate, and evaluate trustworthy synthetic data.",
    },
  ];
}

const promptSuggestions = [
  {
    label: "Insurance claims",
    prompt:
      "Design a synthetic data scenario for 10,000 insurance claims where 15% are denied, with realistic diagnoses, providers, and payment timelines.",
  },
  {
    label: "Patient journeys",
    prompt:
      "Design a scenario for 5,000 longitudinal patient journeys with diagnoses, treatments, follow-ups, and realistic care gaps.",
  },
  {
    label: "E-commerce orders",
    prompt:
      "Plan an e-commerce dataset with 25,000 orders, customers, products, refunds, and seasonal purchasing patterns.",
  },
];

// metadata.scenarioId links a chat scenario card to its one saved scenario (persisted with the conversation).
// The Flower hops above its box, so don't clip it to the avatar circle.
const AGENT_AVATAR_CLASS = "self-start overflow-visible rounded-none bg-transparent";
// ponytail: bot-avatars is ~130 KB; load it after the chat renders, not in the route chunk.
const BotAvatar = lazy(() => import("bot-avatars").then((m) => ({ default: m.BotAvatar })));

function AgentAvatar({ working = false, paused = false }: { working?: boolean; paused?: boolean }) {
  return (
    <Suspense fallback={<span className="block size-8" />}>
      <BotAvatar type="flower" size={32} state={working ? "working" : "default"} paused={paused} aria-hidden />
    </Suspense>
  );
}

type SydaMessage = UIMessage<{ scenarioId?: string }, { scenario: ScenarioConfiguration; status: { message: string } }>;

type ChatRun = ChatRunReference;

type ReferenceRow = Record<string, unknown>;
type ReferenceData = Record<string, ReferenceRow[]>;
type ReferenceAttachment = {
  data: ReferenceData;
  fileName: string;
  schemaSummary: string;
};

function parseReferenceData(text: string): ReferenceData {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("Reference JSON is not valid JSON.");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Reference JSON must be an object whose keys are table names.");
  }

  const entries = Object.entries(parsed as Record<string, unknown>);
  if (!entries.length) throw new Error("Reference JSON must contain at least one table.");

  const referenceData: ReferenceData = {};
  for (const [table, value] of entries) {
    if (!table.trim() || !Array.isArray(value) || !value.length) {
      throw new Error(`Reference table ${table || "(unnamed)"} must contain at least one row.`);
    }
    referenceData[table] = value.map((row, index) => {
      if (!row || typeof row !== "object" || Array.isArray(row) || !Object.keys(row).length) {
        throw new Error(`Reference table ${table} row ${index + 1} must be a non-empty object.`);
      }
      return row as ReferenceRow;
    });
  }
  return referenceData;
}

function parseCsvReferenceData(text: string, fileName: string): ReferenceData {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  const input = text.replace(/^\uFEFF/, "");

  for (let index = 0; index < input.length; index += 1) {
    const character = input[index];
    if (character === '"') {
      if (quoted && input[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      row.push(field.trim());
      field = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      row.push(field.trim());
      if (row.some((value) => value.length > 0)) rows.push(row);
      row = [];
      field = "";
      if (character === "\r" && input[index + 1] === "\n") index += 1;
    } else {
      field += character;
    }
  }
  if (quoted) throw new Error("Reference CSV contains an unclosed quoted value.");
  row.push(field.trim());
  if (row.some((value) => value.length > 0)) rows.push(row);

  if (rows.length < 2) {
    throw new Error("Reference CSV must contain a header and at least one data row.");
  }
  const headers = rows[0];
  if (headers.some((header) => !header)) {
    throw new Error("Reference CSV headers cannot be empty.");
  }
  if (new Set(headers).size !== headers.length) {
    throw new Error("Reference CSV headers must be unique.");
  }

  const table = fileName.replace(/\.csv$/i, "").trim();
  if (!table) throw new Error("Reference CSV filename must identify its table.");
  const referenceRows = rows.slice(1).map((values, index) => {
    if (values.length !== headers.length) {
      throw new Error(`Reference CSV row ${index + 2} must contain ${headers.length} values.`);
    }
    return Object.fromEntries(headers.map((header, column) => [header, values[column] || null]));
  });
  return { [table]: referenceRows };
}

function referenceSchemaSummary(referenceData: ReferenceData) {
  return Object.entries(referenceData).map(([table, rows]) => {
    const fields = [...new Set(rows.flatMap((row) => Object.keys(row)))];
    return `${table}(${fields.join(", ")})`;
  }).join("; ");
}

function titleForPrompt(prompt: string) {
  const normalized = prompt.replace(/\s+/g, " ").trim();
  if (/^(hi|hello|hey|thanks|thank you|ok|okay|test)\W*$/i.test(normalized)) return "New conversation";
  return normalized.length > 72 ? `${normalized.slice(0, 69).trimEnd()}…` : normalized || "New conversation";
}

function isUntitledConversation(title: string) {
  return !title || title === "New conversation" || /^(hi|hello|hey|thanks|thank you|ok|okay|test)\W*$/i.test(title);
}

function firstUserPrompt(messages: SydaMessage[]) {
  const firstUser = messages.find((message) => message.role === "user");
  return firstUser?.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join(" ") ?? "New conversation";
}

let legacyChatMigration: Promise<SavedConversation> | undefined;

function createMigratedConversation(title: string) {
  legacyChatMigration ??= fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  }).then(async (response) => {
    if (!response.ok) throw new Error(`Could not save the previous chat (${response.status}).`);
    return await response.json() as SavedConversation;
  }).finally(() => { legacyChatMigration = undefined; });
  return legacyChatMigration;
}

function PromptComposer({
  value,
  onChange,
  onReferenceAttachment,
  onSubmit,
  onStop,
  referenceFileName,
  compact = false,
  disabled = false,
  isLoading = false,
}: {
  value: string;
  onChange: (value: string) => void;
  onReferenceAttachment: (attachment: ReferenceAttachment) => void;
  onSubmit: () => void;
  onStop: () => void;
  referenceFileName?: string;
  compact?: boolean;
  disabled?: boolean;
  isLoading?: boolean;
}) {
  const [attachmentError, setAttachmentError] = useState("");
  const handleAttachment = async (file: File) => {
    setAttachmentError("");
    if (file.size > 100000 || !/\.(csv|json|txt)$/i.test(file.name)) {
      setAttachmentError("Choose a CSV, JSON, or TXT file under 100 KB.");
      return;
    }
    try {
      const text = await file.text();
      if (/\.(csv|json)$/i.test(file.name)) {
        const data = /\.json$/i.test(file.name)
          ? parseReferenceData(text)
          : parseCsvReferenceData(text, file.name);
        onReferenceAttachment({
          data,
          fileName: file.name,
          schemaSummary: referenceSchemaSummary(data),
        });
        return;
      }
      onChange(value + `\n\nAttached context (${file.name}):\n${text}`);
    } catch (error) {
      setAttachmentError(error instanceof Error ? error.message : "Could not read this file. Please retry.");
    }
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !disabled && !isLoading) {
      event.preventDefault();
      onSubmit();
    }
  };

  return (
    <form
      className="w-full"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (!disabled && !isLoading) {
          onSubmit();
        }
      }}
    >
      <div className="relative overflow-hidden rounded-3xl border bg-card shadow-[0_14px_44px_-28px_oklch(0.2_0.02_260/0.45)] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring/30">
        <Textarea
          aria-label="Describe your synthetic data scenario"
          autoFocus={!compact}
          className={compact ? "min-h-20 resize-none rounded-none border-0 shadow-none focus-visible:ring-0" : "min-h-28 resize-none rounded-none border-0 shadow-none focus-visible:ring-0"}
          disabled={disabled || isLoading}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isLoading ? "Syda Agent is analyzing and compiling your scenario…" : "Describe the data you want to generate…"}
          value={value}
        />
        <div className="flex flex-wrap items-center gap-2 border-t px-3 py-3">
          <label className="inline-flex h-9 shrink-0 cursor-pointer items-center justify-center rounded-full border px-3 text-sm font-medium focus-within:ring-2 focus-within:ring-ring">
            Attach<input className="sr-only" aria-label="Attach schema or sample" disabled={disabled || isLoading} type="file" accept=".csv,.json,.txt" onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void handleAttachment(file); }} />
          </label>
          <Button aria-label={isLoading ? "Stop response" : "Send prompt"} title={isLoading ? "Stop response" : "Send prompt"} className="ml-auto shrink-0" disabled={!isLoading && (!value.trim() || disabled)} size="icon" type={isLoading ? "button" : "submit"} onClick={isLoading ? onStop : undefined}>
          {isLoading ? (
            <span aria-hidden="true" className="size-3 rounded-[2px] bg-current" />
          ) : (
            <HugeiconsIcon icon={ArrowUp02Icon} strokeWidth={2} />
          )}
        </Button>
        </div>
      </div>
      {attachmentError && <p role="alert" className="mt-2 text-xs text-destructive">{attachmentError}</p>}
      {referenceFileName && !attachmentError && <p role="status" className="mt-2 text-xs text-muted-foreground">Reference ready: {referenceFileName}</p>}
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
  onEdit,
  onGenerate,
  messageId,
  sourceName,
}: {
  scenario: ScenarioConfiguration;
  onEdit: (scenario: ScenarioConfiguration, messageId: string) => Promise<void>;
  onGenerate: (scenario: ScenarioConfiguration) => Promise<void>;
  messageId: string;
  sourceName: string;
}) {
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string>();
  const [reviewOpen, setReviewOpen] = useState(false);
  const [estimate, setEstimate] = useState<CostEstimate>();
  const [estimateLoading, setEstimateLoading] = useState(false);
  const [estimateError, setEstimateError] = useState<string>();
  const [generationError, setGenerationError] = useState<string>();

  const openEditor = async () => {
    setSaving(true);
    setSaveError(undefined);
    try {
      await onEdit(scenario, messageId);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Could not save the scenario.");
    } finally {
      setSaving(false);
    }
  };

  const openReview = async () => {
    setReviewOpen(true);
    setEstimate(undefined);
    setEstimateError(undefined);
    setGenerationError(undefined);
    setEstimateLoading(true);
    try {
      const response = await fetch("/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => undefined) as { detail?: string } | undefined;
        throw new Error(typeof body?.detail === "string" ? body.detail : `Could not estimate this run (${response.status}).`);
      }
      setEstimate(await response.json() as CostEstimate);
    } catch (error) {
      setEstimateError(error instanceof Error ? error.message : "Could not estimate this run.");
    } finally {
      setEstimateLoading(false);
    }
  };

  const startGeneration = async () => {
    setSaving(true);
    setGenerationError(undefined);
    try {
      await onGenerate(scenario);
      setReviewOpen(false);
    } catch (error) {
      setGenerationError(error instanceof Error ? error.message : "Could not start dataset generation.");
    } finally {
      setSaving(false);
    }
  };

  const tableEntries = Object.entries(scenario.schemas ?? {});
  const totalPathWeight = scenario.paths?.reduce((total, path) => total + path.weight, 0) ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <Card className="shadow-none">
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>{scenario.title}</CardTitle>
              <CardDescription>{scenario.description}</CardDescription>
            </div>
            <div className="flex flex-wrap gap-2">
              <Badge variant="secondary">Draft plan</Badge>
              <Badge variant="outline">{scenario.recordCount.toLocaleString()} instances</Badge>
              <Badge variant="outline">{Object.keys(scenario.schemas ?? {}).length} tables</Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {!!scenario.workflow.length && <section className="flex flex-col gap-2" aria-labelledby="scenario-workflow">
            <h3 className="text-xs font-medium" id="scenario-workflow">Workflow</h3>
            <div className="flex flex-wrap gap-1.5">
              {scenario.workflow.map((step, index) => (
                <Badge key={`${step}-${index}`} variant="secondary">{index + 1}. {step}</Badge>
              ))}
            </div>
          </section>}
          {!!scenario.paths?.length && (
            <section className="flex flex-col gap-2" aria-labelledby="scenario-paths">
              <h3 className="text-xs font-medium" id="scenario-paths">Scenario paths</h3>
              <div className="flex flex-wrap gap-1.5">
                {scenario.paths.map((path) => (
                  <Badge key={path.name} variant="outline">
                    {path.name} · {Math.round((path.weight / totalPathWeight) * 100)}%
                  </Badge>
                ))}
              </div>
            </section>
          )}
          {!!tableEntries.length && <section className="flex flex-col gap-2" aria-labelledby="scenario-schema">
            <h3 className="text-xs font-medium" id="scenario-schema">Schema preview</h3>
            <details><summary className="cursor-pointer text-xs">Browse {tableEntries.length} tables and columns</summary><div className="mt-3"><SchemaExplorer schemas={scenario.schemas ?? {}} /></div></details>
          </section>}
          {!!scenario.rules.length && <section className="flex flex-col gap-2" aria-labelledby="scenario-rules">
            <h3 className="text-xs font-medium" id="scenario-rules">Rules</h3>
            <ul className="list-disc pl-4 text-xs text-muted-foreground">
              {scenario.rules.map((rule, index) => <li key={`${rule}-${index}`}>{rule}</li>)}
            </ul>

          </section>}
        </CardContent>
        <CardFooter className="flex-wrap justify-between gap-2">
          <Button disabled={saving} onClick={() => void openEditor()} type="button" variant="outline">
            {saving ? <Spinner data-icon="inline-start" /> : null}{saving ? "Opening editor…" : "Edit full scenario"}
          </Button>
          <Button onClick={() => void openReview()} type="button">
            <HugeiconsIcon data-icon="inline-start" icon={SparklesIcon} strokeWidth={2} /> Review plan
          </Button>
        </CardFooter>
      </Card>

      {saveError && <Alert variant="destructive"><AlertTitle>Could not open editor</AlertTitle><AlertDescription>{saveError}</AlertDescription></Alert>}

      <Dialog onOpenChange={setReviewOpen} open={reviewOpen}>
        <DialogContent className="max-h-[calc(100svh-2rem)] max-w-lg overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Review before generating</DialogTitle>
            <DialogDescription>{scenario.title}</DialogDescription>
          </DialogHeader>
          <div className="space-y-5">
            <dl className="grid grid-cols-2 gap-3 rounded-2xl border bg-muted/30 p-4 text-xs">
              <div><dt className="text-muted-foreground">Scenario instances</dt><dd className="mt-1 font-medium tabular-nums">{scenario.recordCount.toLocaleString()}</dd></div>
              <div><dt className="text-muted-foreground">Connected tables</dt><dd className="mt-1 font-medium tabular-nums">{tableEntries.length || scenario.workflow.length}</dd></div>
              <div><dt className="text-muted-foreground">Workflow steps</dt><dd className="mt-1 font-medium tabular-nums">{scenario.workflow.length}</dd></div>
              <div><dt className="text-muted-foreground">Rules</dt><dd className="mt-1 font-medium tabular-nums">{scenario.rules.length}</dd></div>
            </dl>
            <section className="rounded-2xl border p-4 text-xs space-y-2"><h3 className="font-medium">Source and destination</h3><p>Source: {sourceName}</p><p>Destination: downloadable CSV files</p></section>
            <section className="rounded-2xl border p-4">
              <div className="flex items-start justify-between gap-3">
                <div><h3 className="text-xs font-medium">Generation estimate</h3><p className="mt-1 text-[10px] text-muted-foreground">{estimate ? `${estimate.provider} · ${estimate.model}` : "Checking configured provider…"}</p></div>
                <p className="text-sm font-semibold tabular-nums">{estimateLoading ? "…" : estimate?.estimatedCostUsd === 0 ? "No model cost" : estimate?.estimatedCostUsd == null ? "Unavailable" : `~$${estimate.estimatedCostUsd.toFixed(4)}`}</p>
              </div>
              {(estimate?.note || estimateError) && <p className="mt-2 text-[10px] leading-4 text-muted-foreground">{estimateError ?? estimate?.note}</p>}
            </section>
            {generationError && <Alert variant="destructive"><AlertTitle>Could not start generation</AlertTitle><AlertDescription>{generationError}</AlertDescription></Alert>}
          </div>
          <DialogFooter className="sm:justify-between">
            <Button disabled={saving} onClick={() => void openEditor()} type="button" variant="outline">Edit full scenario</Button>
            <Button disabled={saving} onClick={() => void startGeneration()} type="button">
              {saving ? <Spinner data-icon="inline-start" /> : null}{saving ? "Starting dataset…" : "Generate dataset"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

    </div>
  );
}

function GenerationRunCard({ run }: { run: ChatRun }) {
  const [job, setJob] = useState<GenerationJob>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const response = await fetch(`/api/jobs/${run.jobId}`);
        if (!response.ok) throw new Error(`Could not load run status (${response.status}).`);
        const nextJob = await response.json() as GenerationJob;
        if (cancelled) return;
        setJob(nextJob);
        setError(undefined);
        if (!(["complete", "failed"].includes(nextJob.status))) {
          timer = window.setTimeout(() => void refresh(), 3000);
        }
      } catch (fetchError) {
        if (!cancelled) {
          setError(fetchError instanceof Error ? fetchError.message : "Run status is unavailable.");
          timer = window.setTimeout(() => void refresh(), 5000);
        }
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [run.jobId]);

  const status = job?.status ?? "queued";
  const complete = status === "complete";
  const failed = status === "failed";

  return (
    <Card className="border-primary/20 shadow-none">
      <CardHeader>
        <CardTitle>{complete ? "Dataset ready" : failed ? "Generation failed" : "Generating dataset"}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!complete && !failed && <Progress value={job?.progress ?? 10}><ProgressLabel>{job?.currentStage ?? "Starting generation worker…"}</ProgressLabel><ProgressValue>{(value) => value}</ProgressValue></Progress>}
        {(error || (failed && job?.currentStage)) && <p className="text-xs text-destructive">{error ?? job?.currentStage}</p>}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-[10px] text-muted-foreground">Run {run.jobId}</p>
          <div className="flex flex-wrap gap-2">
            {complete && (job?.downloadUrl || job?.filesAvailable) && <a className={buttonVariants({ size: "sm" })} href={job?.downloadUrl ?? `/api/download/${run.jobId}`}>Download dataset</a>}
            <Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/runs/${run.jobId}?tab=data`}>Browse full dataset</Link>
          </div>
        </div>
        {(complete || failed) && job?.stats && <RunQualitySummary stats={job.stats} scenario={run.scenario} />}
        {failed && <Link className={buttonVariants({ size: "sm", variant: "outline" })} to={`/scenarios/${run.scenarioId}`}>Review scenario and retry</Link>}
        {complete && job?.filesAvailable && <DataPreview compact complete jobId={run.jobId} showDownloads={false} />}
      </CardContent>
    </Card>
  );
}

export default function Home() {
  const navigate = useNavigate();
  const location = useLocation();
  const { conversationId } = useParams();
  const conversationIdRef = useRef<string | undefined>(conversationId);
  conversationIdRef.current = conversationId;
  const [prompt, setPrompt] = useState("");
  const [source, setSource] = useState<SchemaSource | null>(null);
  const [referenceAttachment, setReferenceAttachment] = useState<ReferenceAttachment>();
  const [chatRuns, setChatRuns] = useState<ChatRun[]>([]);
  const [conversationTitle, setConversationTitle] = useState("");
  const [loadedConversationId, setLoadedConversationId] = useState<string>();
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationError, setConversationError] = useState<string>();
  const skipConversationLoadRef = useRef<string | undefined>(undefined);
  const editorReturnRef = useRef<{
    editedScenario?: ScenarioConfiguration;
    scenarioId?: string;
    messageId: string;
    generatedRun?: ChatRun;
  } | undefined>(undefined);
  const persistedSnapshotRef = useRef("");
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const chatTransport = useMemo(() => new DefaultChatTransport<SydaMessage>({
    api: "/api/agent/chat",
    fetch: createChatFetch(),
    prepareSendMessagesRequest: ({ body, id, messages, trigger, messageId }) => ({
      body: {
        ...body,
        id,
        messages,
        trigger,
        messageId,
        conversationId: conversationIdRef.current,
      },
    }),
  }), []);
  // Live tool progress ("Drafting the scenario…"); transient, never saved to the transcript.
  const [agentStatus, setAgentStatus] = useState<string>();
  const { clearError, error, messages, sendMessage, setMessages, status, stop } = useChat<SydaMessage>({
    transport: chatTransport,
    onData: (part) => {
      if (part.type === "data-status") setAgentStatus(part.data.message);
    },
  });
  const [isAwaitingResponse, setIsAwaitingResponse] = useState(false);
  const isThinking = status === "submitted" || status === "streaming" || isAwaitingResponse;

  useEffect(() => {
    const returnState = location.state as {
      schemaSource?: SchemaSource;
      editedScenario?: ScenarioConfiguration;
      scenarioId?: string;
      messageId?: string;
      generatedRun?: ChatRun;
    } | null;
    if (returnState?.schemaSource) {
      setSource(returnState.schemaSource);
      navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
      return;
    }
    if (!returnState?.messageId || (!returnState.editedScenario && !returnState.generatedRun)) return;
    editorReturnRef.current = { ...returnState, messageId: returnState.messageId };
    navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
  }, [location.key, location.pathname, location.search, location.state, navigate]);

  useEffect(() => {
    const returnState = editorReturnRef.current;
    if (!returnState || (conversationId && loadedConversationId !== conversationId)) return;
    const savedId = returnState.scenarioId ?? returnState.generatedRun?.scenarioId;
    setMessages((currentMessages) => currentMessages.map((message) => message.id === returnState.messageId
      ? {
          ...message,
          metadata: savedId ? { ...message.metadata, scenarioId: savedId } : message.metadata,
          parts: returnState.editedScenario
            ? message.parts.map((part) => part.type === "data-scenario" ? { ...part, data: returnState.editedScenario! } : part)
            : message.parts,
        }
      : message));
    if (returnState.generatedRun) {
      setChatRuns((runs) => runs.some((run) => run.jobId === returnState.generatedRun!.jobId)
        ? runs
        : [...runs, returnState.generatedRun!]);
    }
    editorReturnRef.current = undefined;
  }, [conversationId, loadedConversationId, setMessages]);

  const latestScenario = messages
    .flatMap((message) => message.parts)
    .filter((part) => part.type === "data-scenario")
    .at(-1)?.data;

  useEffect(() => {
    let active = true;
    if (conversationId && skipConversationLoadRef.current === conversationId) {
      skipConversationLoadRef.current = undefined;
      return () => { active = false; };
    }

    setReferenceAttachment(undefined);
    stop();
    setIsAwaitingResponse(false);
    setConversationError(undefined);
    if (!conversationId) {
      setConversationLoading(false);
      setConversationTitle("");
      setLoadedConversationId(undefined);
      conversationIdRef.current = undefined;
      persistedSnapshotRef.current = "";

      // Bring forward the last tab-local transcript once so the new durable
      // history feature does not discard an in-progress pre-history chat.
      let legacyMessages: SydaMessage[] = [];
      let legacyRuns: ChatRun[] = [];
      try {
        const parsedMessages = JSON.parse(sessionStorage.getItem("syda:chat-messages") ?? "[]");
        const parsedRuns = JSON.parse(sessionStorage.getItem("syda:chat-runs") ?? "[]");
        if (Array.isArray(parsedMessages)) legacyMessages = parsedMessages as SydaMessage[];
        if (Array.isArray(parsedRuns)) legacyRuns = parsedRuns as ChatRun[];
      } catch {
        sessionStorage.removeItem("syda:chat-messages");
        sessionStorage.removeItem("syda:chat-runs");
      }

      if (legacyMessages.length && !sessionStorage.getItem("syda:legacy-chat-migrated")) {
        const title = titleForPrompt(firstUserPrompt(legacyMessages));
        void createMigratedConversation(title).then((conversation) => {
          if (!active) return;
          skipConversationLoadRef.current = conversation.id;
          setConversationTitle(conversation.title);
          setChatRuns(legacyRuns);
          setMessages(legacyMessages);
          setLoadedConversationId(conversation.id);
          conversationIdRef.current = conversation.id;
          persistedSnapshotRef.current = JSON.stringify({ title: conversation.title, messages: [], runs: [], scenarioDraft: undefined });
          sessionStorage.setItem("syda:legacy-chat-migrated", "true");
          sessionStorage.removeItem("syda:chat-messages");
          sessionStorage.removeItem("syda:chat-runs");
          window.dispatchEvent(new Event("syda:conversations-changed"));
          navigate(`/app/${conversation.id}`, { replace: true });
        }).catch((migrationError) => {
          if (active) setConversationError(migrationError instanceof Error ? migrationError.message : "Could not migrate the previous chat.");
        });
      } else {
        setMessages([]);
        setChatRuns([]);
      }
      return () => { active = false; };
    }

    setConversationLoading(true);
    setLoadedConversationId(undefined);
    void fetch(`/api/conversations/${conversationId}`)
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 404 ? "Chat not found." : `Could not load chat (${response.status}).`);
        return await response.json() as SavedConversation;
      })
      .then((conversation) => {
        if (!active) return;
        const restoredMessages = [...conversation.messages] as SydaMessage[];
        const lastMsg = restoredMessages.length > 0 ? restoredMessages[restoredMessages.length - 1] : null;
        setIsAwaitingResponse(lastMsg?.role === "user");
        if (conversation.scenarioDraft) {
          let assistantIndex = -1;
          for (let index = restoredMessages.length - 1; index >= 0; index -= 1) {
            if (restoredMessages[index].role === "assistant") {
              assistantIndex = index;
              break;
            }
          }
          const assistantMessage = assistantIndex >= 0
            ? restoredMessages[assistantIndex]
            : { id: crypto.randomUUID(), role: "assistant" as const, parts: [] };
          const scenarioPart = { type: "data-scenario" as const, id: "scenario", data: conversation.scenarioDraft };
          const parts = assistantMessage.parts.filter((part) => part.type !== "data-scenario") as SydaMessage["parts"];
          parts.push(scenarioPart as SydaMessage["parts"][number]);
          const restoredAssistant = { ...assistantMessage, parts } as SydaMessage;
          if (assistantIndex >= 0) restoredMessages[assistantIndex] = restoredAssistant;
          else restoredMessages.push(restoredAssistant);
        }
        const restoredRuns = conversation.runs ?? [];
        setConversationTitle(conversation.title);
        setMessages(restoredMessages);
        setChatRuns(restoredRuns);
        setLoadedConversationId(conversation.id);
        conversationIdRef.current = conversation.id;
        persistedSnapshotRef.current = JSON.stringify({
          title: conversation.title,
          messages: restoredMessages,
          runs: restoredRuns,
          scenarioDraft: conversation.scenarioDraft ?? undefined,
        });
      })
      .catch((loadError) => {
        if (active) setConversationError(loadError instanceof Error ? loadError.message : "Could not load chat history.");
      })
      .finally(() => { if (active) setConversationLoading(false); });
    return () => { active = false; };
  }, [conversationId, navigate, setMessages, stop]);

  useEffect(() => {
    if (!conversationId || !isAwaitingResponse) return;
    let active = true;
    const interval = setInterval(() => {
      fetch(`/api/conversations/${conversationId}`)
        .then(async (response) => {
          if (!response.ok) return;
          const conversation = await response.json() as SavedConversation;
          if (!active) return;
          const restoredMessages = [...conversation.messages] as SydaMessage[];
          const lastMsg = restoredMessages.length > 0 ? restoredMessages[restoredMessages.length - 1] : null;
          if (lastMsg?.role === "assistant") {
            setIsAwaitingResponse(false);
            if (conversation.scenarioDraft) {
              let assistantIndex = -1;
              for (let index = restoredMessages.length - 1; index >= 0; index -= 1) {
                if (restoredMessages[index].role === "assistant") {
                  assistantIndex = index;
                  break;
                }
              }
              const assistantMessage = assistantIndex >= 0
                ? restoredMessages[assistantIndex]
                : { id: crypto.randomUUID(), role: "assistant" as const, parts: [] };
              const scenarioPart = { type: "data-scenario" as const, id: "scenario", data: conversation.scenarioDraft };
              const parts = assistantMessage.parts.filter((part) => part.type !== "data-scenario") as SydaMessage["parts"];
              parts.push(scenarioPart as SydaMessage["parts"][number]);
              const restoredAssistant = { ...assistantMessage, parts } as SydaMessage;
              if (assistantIndex >= 0) restoredMessages[assistantIndex] = restoredAssistant;
              else restoredMessages.push(restoredAssistant);
            }
            setMessages(restoredMessages);
            setChatRuns(conversation.runs ?? []);
            persistedSnapshotRef.current = JSON.stringify({
              title: conversation.title,
              messages: restoredMessages,
              runs: conversation.runs ?? [],
              scenarioDraft: conversation.scenarioDraft ?? undefined,
            });
            window.dispatchEvent(new Event("syda:conversations-changed"));
          }
        })
        .catch(() => {});
    }, 2000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [conversationId, isAwaitingResponse, setMessages]);

  useEffect(() => {
    if (!conversationId || loadedConversationId !== conversationId || status === "streaming") return;
    const payload = { title: conversationTitle, messages, runs: chatRuns, scenarioDraft: latestScenario };
    const snapshot = JSON.stringify(payload);
    if (snapshot === persistedSnapshotRef.current) return;
    saveQueueRef.current = saveQueueRef.current
      .catch(() => {})
      .then(async () => {
        const response = await fetch(`/api/conversations/${conversationId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: snapshot,
        });
        if (!response.ok) throw new Error(`Could not save chat history (${response.status}).`);
        persistedSnapshotRef.current = snapshot;
        setConversationError(undefined);
        window.dispatchEvent(new Event("syda:conversations-changed"));
      })
      .catch((saveError) => {
        setConversationError(saveError instanceof Error ? saveError.message : "Could not save chat history.");
      });
  }, [chatRuns, conversationId, conversationTitle, latestScenario, loadedConversationId, messages, status]);

  useEffect(() => () => {
    stop();
  }, [stop]);

  const submitPrompt = async (promptOverride?: string) => {
    const text = (typeof promptOverride === "string" ? promptOverride : prompt).trim();
    if (!text || isThinking || conversationLoading || (conversationId && loadedConversationId !== conversationId)) return;

    setConversationError(undefined);
    setAgentStatus(undefined);
    if (!conversationId) {
      try {
        const response = await fetch("/api/conversations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: titleForPrompt(text) }),
        });
        if (!response.ok) throw new Error(`Could not save this chat (${response.status}).`);
        const conversation = await response.json() as SavedConversation;
        skipConversationLoadRef.current = conversation.id;
        conversationIdRef.current = conversation.id;
        setConversationTitle(conversation.title);
        setChatRuns([]);
        setLoadedConversationId(conversation.id);
        persistedSnapshotRef.current = JSON.stringify({ title: conversation.title, messages: [], runs: [], scenarioDraft: undefined });
        window.dispatchEvent(new Event("syda:conversations-changed"));
        navigate(`/app/${conversation.id}`, { replace: true });
      } catch (createError) {
        setConversationError(createError instanceof Error ? createError.message : "Could not save this chat.");
        return;
      }
    }

    if (conversationId && isUntitledConversation(conversationTitle) && !isUntitledConversation(text)) {
      setConversationTitle(titleForPrompt(text));
    }

    const referenceContext = referenceAttachment
      ? `\n\nAttached reference schema (${referenceAttachment.fileName}): ${referenceAttachment.schemaSummary}. Reference rows will be supplied separately during generation.`
      : "";
    setPrompt("");
    clearError();
    const promptWithReference = `${text}${referenceContext}`;
    await sendMessage({ text: source ? `${promptWithReference}\n\nDatabase schema context (${source.name}; structure only, output CSV):\n${JSON.stringify(source.schemas)}` : promptWithReference });
  };

  const startNewScenario = () => {
    setSource(null);
    setReferenceAttachment(undefined);
    stop();
    setIsAwaitingResponse(false);
    setPrompt("");
    setMessages([]);
    setChatRuns([]);
    setConversationTitle("");
    setLoadedConversationId(undefined);
    setConversationError(undefined);
    conversationIdRef.current = undefined;
    persistedSnapshotRef.current = "";
    navigate("/app");
  };

  const saveMessageScenario = async (scenario: ScenarioConfiguration, messageId: string) => {
    const savedId = messages.find((message) => message.id === messageId)?.metadata?.scenarioId;
    const id = await saveScenario(scenario, savedId);
    window.dispatchEvent(new Event("syda:scenarios-changed"));
    if (id !== savedId) {
      setMessages((currentMessages) => currentMessages.map((message) => message.id === messageId
        ? { ...message, metadata: { ...message.metadata, scenarioId: id } }
        : message));
    }
    return id;
  };

  const openEditor = async (scenario: ScenarioConfiguration, messageId: string) => {
    // ponytail: the editor hands scenarioId back on return; leaving it with browser Back loses that link for a first save.
    const id = await saveMessageScenario(scenario, messageId);
    const returnTo = conversationId ? `/app/${conversationId}` : "/app";
    navigate(`/scenarios/${id}?returnTo=${encodeURIComponent(returnTo)}&messageId=${encodeURIComponent(messageId)}`);
  };

  const startGeneration = async (scenario: ScenarioConfiguration, messageId: string) => {
    const validationResponse = await fetch("/api/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario }),
    });
    if (!validationResponse.ok) throw new Error(`Could not validate this scenario (${validationResponse.status}).`);
    const validation = await validationResponse.json() as { valid: boolean; schema_errors?: string[] };
    if (!validation.valid) throw new Error(validation.schema_errors?.join(" ") || "Fix the scenario before generating.");

    const scenarioId = await saveMessageScenario(scenario, messageId);

    try {
      const response = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario,
          scenarioId,
          ...(referenceAttachment ? { referenceData: referenceAttachment.data } : {}),
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => undefined) as { detail?: string } | undefined;
        throw new Error(body?.detail ?? `Could not start generation (${response.status}).`);
      }
      const job = await response.json() as { jobId: string };
      setChatRuns((runs) => [...runs, { messageId, jobId: job.jobId, scenarioId, scenario }]);
    } catch (error) {
      throw new Error(`The scenario was saved, but generation could not start. ${error instanceof Error ? error.message : "Try again from the scenario editor."}`);
    }
  };

  return (
    <AppShell title={conversationTitle || latestScenario?.title || "New scenario"} onNewScenario={startNewScenario}>

        {conversationError && <div className="mx-auto mt-4 w-full max-w-3xl px-5"><Alert variant="destructive"><AlertTitle>Chat history issue</AlertTitle><AlertDescription>{conversationError}</AlertDescription></Alert></div>}

        {conversationLoading ? (
          <main className="flex min-h-0 flex-1 items-center justify-center gap-2 text-sm text-muted-foreground"><Spinner /> Loading chat history…</main>
        ) : !messages.length ? (
          <main className="relative flex min-h-0 flex-1 items-center justify-center overflow-y-auto px-5 py-12">
            <div aria-hidden="true" className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_38%,oklch(0.96_0.025_265),transparent_38%)] dark:bg-[radial-gradient(circle_at_50%_38%,oklch(0.24_0.03_265),transparent_42%)]" />
            <div className="relative flex w-full max-w-2xl flex-col items-center">
              <div className="mb-5 flex size-10 items-center justify-center border bg-card shadow-sm">
                <HugeiconsIcon icon={SparklesIcon} strokeWidth={1.8} />
              </div>
              <h1 className="max-w-lg text-center text-3xl font-semibold tracking-[-0.035em] sm:text-4xl">What should we generate?</h1>
              <p className="mb-8 mt-3 max-w-md text-center text-sm leading-6 text-muted-foreground">
                Describe the entities, relationships, constraints, and edge cases. Syda will turn them into a testable scenario.
              </p>
              <PromptComposer disabled={isThinking || conversationLoading || (!!conversationId && loadedConversationId !== conversationId)} isLoading={isThinking} onChange={setPrompt} onReferenceAttachment={setReferenceAttachment} onSubmit={submitPrompt} onStop={() => { stop(); setIsAwaitingResponse(false); }} referenceFileName={referenceAttachment?.fileName} value={prompt} />
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
                    {messages.map((message, index) => {
                      const text = message.parts
                        .filter((part) => part.type === "text")
                        .map((part) => part.text)
                        .join("");
                      const scenarioPart = message.parts.find((part) => part.type === "data-scenario");
                      const candidateScenario = scenarioPart?.data;
                      const scenario = candidateScenario
                        && candidateScenario.workflow?.length >= 2
                        && candidateScenario.rules?.length > 0
                        && Object.keys(candidateScenario.schemas ?? {}).length > 0
                        ? candidateScenario
                        : undefined;
                      const isStreamingThis = status === "streaming" && index === messages.length - 1 && message.role === "assistant";

                      return (
                        <MessageScrollerItem
                          key={message.id}
                          messageId={message.id}
                          scrollAnchor={message.role === "user"}
                        >
                          <Message align={message.role === "user" ? "end" : "start"}>
                            {message.role === "assistant" && (
                              <MessageAvatar className={AGENT_AVATAR_CLASS}>
                                {/* ponytail: only the newest reply animates; a long chat doesn't run a canvas per message. */}
                                <AgentAvatar paused={index !== messages.length - 1} />
                              </MessageAvatar>
                            )}
                            <MessageContent>
                              <Bubble align={message.role === "user" ? "end" : "start"} className={scenario ? "w-full" : undefined} variant={message.role === "user" ? "secondary" : "ghost"}>
                                <BubbleContent className={scenario ? "w-full" : undefined}>
                                  <CleanMessageText
                                    text={text}
                                    isStreaming={isStreamingThis}
                                    isUser={message.role === "user"}
                                    hasScenario={Boolean(scenario)}
                                  />
                                  <div className="flex flex-col gap-6">
                                    {scenario && (
                                      <ScenarioCard
                                        sourceName={messages.slice(0, index).reverse().find(item => item.role === "user")?.parts.filter(part => part.type === "text").map(part => part.text).join(" ").match(/Database schema context \((.*); structure only, output CSV\):/)?.[1] ?? "Chat instructions"}
                                        messageId={message.id}
                                        onEdit={openEditor}
                                        onGenerate={(nextScenario) => startGeneration(nextScenario, message.id)}
                                        scenario={scenario}
                                      />
                                    )}
                                    {chatRuns.filter((run) => run.messageId === message.id).map((run) => <GenerationRunCard key={run.jobId} run={run} />)}
                                  </div>
                                </BubbleContent>
                              </Bubble>
                            </MessageContent>
                          </Message>
                        </MessageScrollerItem>
                      );
                    })}
                    {(status === "submitted" || isAwaitingResponse) && (
                      <MessageScrollerItem messageId="thinking-indicator" scrollAnchor>
                          <Message align="start">
                          <MessageAvatar className={AGENT_AVATAR_CLASS}>
                            <AgentAvatar working />
                          </MessageAvatar>
                          <MessageContent>
                            <Bubble align="start" className="w-full" variant="ghost">
                              <BubbleContent className="w-full">
                                <div className="flex items-center gap-2 py-1 text-xs text-muted-foreground">
                                  <span className="shimmer" aria-live="polite">{agentStatus ?? "Thinking…"}</span>
                                </div>
                              </BubbleContent>
                            </Bubble>
                          </MessageContent>
                        </Message>
                      </MessageScrollerItem>
                    )}
                  </MessageScrollerContent>
                </MessageScrollerViewport>
                <MessageScrollerButton />
              </MessageScroller>
            </MessageScrollerProvider>
            <div className="shrink-0 border-t bg-background px-5 py-4">
              <div className="mx-auto w-full max-w-3xl">
                <div className="flex items-end gap-2">
                  <PromptComposer compact disabled={isThinking || conversationLoading || (!!conversationId && loadedConversationId !== conversationId)} isLoading={isThinking} onChange={setPrompt} onReferenceAttachment={setReferenceAttachment} onSubmit={submitPrompt} onStop={() => { stop(); setIsAwaitingResponse(false); }} referenceFileName={referenceAttachment?.fileName} value={prompt} />
                </div>
                {error && <p className="mt-2 text-xs text-destructive">{error.message}</p>}
              </div>
            </div>
          </main>
        )}
    </AppShell>
  );
}
