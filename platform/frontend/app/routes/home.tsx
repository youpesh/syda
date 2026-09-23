import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { useChat } from "@ai-sdk/react";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  Add01Icon,
  AiMagicIcon,
  Analytics01Icon,
  ArrowUp02Icon,
  SparklesIcon,
} from "@hugeicons/core-free-icons";

import type { Route } from "./+types/home";
import { Button } from "~/components/ui/button";
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "~/components/ui/dialog";
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
import { Spinner } from "~/components/ui/spinner";
import { Progress, ProgressLabel, ProgressValue } from "~/components/ui/progress";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "~/components/ui/tabs";
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
import { DataPreview } from "~/components/studio/data-preview";
import { EvaluationReport } from "~/components/studio/evaluation-report";
import { ScenarioConfigurator } from "~/components/studio/scenario-configurator";
import type { JobStats, ScenarioConfiguration } from "~/lib/studio-types";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "Syda — Synthetic data, shaped by you" },
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

const promptSuggestions = [
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
  | "complete"
  | "failed";

type ScenarioGeneration = {
  status: GenerationStatus;
  jobId?: string;
  stats?: JobStats;
  downloadUrl?: string;
  error?: string;
  progress?: number;
  currentStage?: string;
};

type SydaMessage = UIMessage<unknown, { scenario: ScenarioConfiguration }>;

const chatTransport = new DefaultChatTransport<SydaMessage>({
  api: "/api/agent/chat",
});

function PromptComposer({
  value,
  onChange,
  onSubmit,
  compact = false,
  disabled = false,
  isLoading = false,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  compact?: boolean;
  disabled?: boolean;
  isLoading?: boolean;
}) {
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
      <div className="border bg-card shadow-[0_14px_44px_-28px_oklch(0.2_0.02_260/0.45)] focus-within:border-ring focus-within:ring-1 focus-within:ring-ring/30">
        <Textarea
          aria-label="Describe your synthetic data scenario"
          autoFocus={!compact}
          className={compact ? "min-h-20 resize-none border-0" : "min-h-28 resize-none border-0"}
          disabled={disabled || isLoading}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isLoading ? "Syda Agent is analyzing and compiling your scenario…" : "Describe the data you want to generate…"}
          value={value}
        />
        <div className="flex items-center justify-between gap-3 px-2.5 pb-2.5">
          <span className="hidden text-xs text-muted-foreground sm:inline">
            {isLoading ? "Compiling scenario rules…" : "Enter to send · Shift + Enter for a new line"}
          </span>
          <Button aria-label="Send prompt" disabled={!value.trim() || disabled || isLoading} size="icon" type="submit">
            {isLoading ? (
              <Spinner />
            ) : (
              <HugeiconsIcon icon={ArrowUp02Icon} strokeWidth={2} />
            )}
          </Button>
        </div>
      </div>
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
  stats,
  jobId,
  error,
  progress,
  currentStage,
  onGenerate,
}: {
  scenario: ScenarioConfiguration;
  status: GenerationStatus;
  stats?: JobStats;
  jobId?: string;
  error?: string;
  progress?: number;
  currentStage?: string;
  onGenerate: (scenario: ScenarioConfiguration) => void;
}) {
  const [draft, setDraft] = useState(scenario);
  const [editDraft, setEditDraft] = useState(scenario);
  const [configurationOpen, setConfigurationOpen] = useState(false);
  const isRunning = ["generating", "validating", "evaluating"].includes(status);

  useEffect(() => {
    setDraft(scenario);
    setEditDraft(scenario);
  }, [scenario]);

  const openConfiguration = () => {
    setEditDraft(draft);
    setConfigurationOpen(true);
  };

  const saveConfiguration = () => {
    setDraft(editDraft);
    setConfigurationOpen(false);
  };

  const tableEntries = Object.entries(draft.schemas ?? {});
  const totalPathWeight = draft.paths?.reduce((total, path) => total + path.weight, 0) ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle>{draft.title}</CardTitle>
              <CardDescription>{draft.description}</CardDescription>
            </div>
            <div className="flex flex-wrap gap-2">
              <Badge variant="outline">{draft.recordCount.toLocaleString()} instances</Badge>
              <Badge variant="outline">{Object.keys(draft.schemas ?? {}).length} tables</Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <section className="flex flex-col gap-2" aria-labelledby="scenario-workflow">
            <h3 className="text-xs font-medium" id="scenario-workflow">Workflow</h3>
            <div className="flex flex-wrap gap-1.5">
              {draft.workflow.map((step, index) => (
                <Badge key={`${step}-${index}`} variant="secondary">{index + 1}. {step}</Badge>
              ))}
            </div>
          </section>
          {!!draft.paths?.length && (
            <section className="flex flex-col gap-2" aria-labelledby="scenario-paths">
              <h3 className="text-xs font-medium" id="scenario-paths">Scenario paths</h3>
              <div className="flex flex-wrap gap-1.5">
                {draft.paths.map((path) => (
                  <Badge key={path.name} variant="outline">
                    {path.name} · {Math.round((path.weight / totalPathWeight) * 100)}%
                  </Badge>
                ))}
              </div>
            </section>
          )}
          <section className="flex flex-col gap-2" aria-labelledby="scenario-schema">
            <h3 className="text-xs font-medium" id="scenario-schema">Schema preview</h3>
            <div className="flex flex-wrap gap-1.5">
              {tableEntries.map(([tableName, table]) => (
                <Badge key={tableName} variant="outline">
                  {tableName} · {Object.keys(table).filter((field) => !field.startsWith("__")).length} fields
                </Badge>
              ))}
            </div>
          </section>
          <section className="flex flex-col gap-2" aria-labelledby="scenario-rules">
            <h3 className="text-xs font-medium" id="scenario-rules">Rules</h3>
            <ul className="list-disc pl-4 text-xs text-muted-foreground">
              {draft.rules.slice(0, 3).map((rule, index) => <li key={`${rule}-${index}`}>{rule}</li>)}
            </ul>
            {draft.rules.length > 3 && <p className="text-xs text-muted-foreground">+{draft.rules.length - 3} more rules</p>}
          </section>
        </CardContent>
        <CardFooter className="justify-end gap-2">
          <Button disabled={isRunning} onClick={openConfiguration} type="button" variant="outline">
            Edit configuration
          </Button>
          <Button disabled={isRunning} onClick={() => onGenerate(draft)} type="button">
            {isRunning ? <Spinner data-icon="inline-start" /> : <HugeiconsIcon data-icon="inline-start" icon={SparklesIcon} strokeWidth={2} />}
            {status === "failed" ? "Retry generation" : status === "complete" ? "Generate again" : isRunning ? "Generating…" : "Generate dataset"}
          </Button>
        </CardFooter>
      </Card>

      <Dialog onOpenChange={setConfigurationOpen} open={configurationOpen}>
        <DialogContent className="max-h-[calc(100svh-2rem)] grid-rows-[auto_minmax(0,1fr)_auto] sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>Edit generation configuration</DialogTitle>
            <DialogDescription>
              Review the schema, workflow, rules, validation, and estimated run cost before generation.
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 overflow-y-auto pr-1">
            <ScenarioConfigurator onChange={setEditDraft} scenario={editDraft} />
          </div>
          <DialogFooter>
            <Button onClick={() => setConfigurationOpen(false)} type="button" variant="outline">Cancel</Button>
            <Button onClick={saveConfiguration} type="button">Save configuration</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {isRunning && (
        <Card size="sm">
          <CardContent>
            <Progress value={progress ?? 10}>
              <ProgressLabel>{currentStage ?? "Preparing generation…"}</ProgressLabel>
              <ProgressValue>{(formattedValue) => formattedValue}</ProgressValue>
            </Progress>
          </CardContent>
        </Card>
      )}

      {status === "failed" && (
        <Alert variant="destructive">
          <AlertTitle>Generation failed</AlertTitle>
          <AlertDescription>
            {error ?? "The generation service could not complete this dataset. Review the configuration and retry."}
          </AlertDescription>
        </Alert>
      )}

      {status === "complete" && (
        <>
          <Alert>
            <AlertTitle>{(stats?.recordsGenerated ?? draft.recordCount).toLocaleString()} records generated</AlertTitle>
            <AlertDescription>
              {stats?.pathCounts && Object.keys(stats.pathCounts).length
                ? `${Object.values(stats.pathCounts).reduce((total, count) => total + count, 0).toLocaleString()} scenario instances completed. `
                : ""}
              Preview and download the generated tables or inspect the quality-gate report.
            </AlertDescription>
          </Alert>
          <Tabs defaultValue="data">
            <TabsList variant="line">
              <TabsTrigger value="data">Data preview</TabsTrigger>
              <TabsTrigger value="evaluation">Evaluation</TabsTrigger>
            </TabsList>
            <TabsContent className="pt-2" value="data">
              <DataPreview complete jobId={jobId} />
            </TabsContent>
            <TabsContent className="pt-2" value="evaluation">
              <EvaluationReport jobId={jobId} stats={stats} />
            </TabsContent>
          </Tabs>
        </>
      )}
    </div>
  );
}

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [generationByMessage, setGenerationByMessage] = useState<Record<string, ScenarioGeneration>>({});
  const generationTimers = useRef<number[]>([]);
  const { clearError, error, messages, sendMessage, setMessages, status, stop } = useChat<SydaMessage>({
    transport: chatTransport,
  });
  const isThinking = status === "submitted" || status === "streaming";
  const latestScenario = messages
    .flatMap((message) => message.parts)
    .filter((part) => part.type === "data-scenario")
    .at(-1)?.data;

  const clearGenerationTimers = () => {
    generationTimers.current.forEach((timer) => {
      window.clearTimeout(timer);
      window.clearInterval(timer);
    });
    generationTimers.current = [];
  };

  useEffect(() => () => {
    clearGenerationTimers();
    stop();
  }, [stop]);

  const submitPrompt = async (promptOverride?: string) => {
    const text = (typeof promptOverride === "string" ? promptOverride : prompt).trim();
    if (!text || isThinking) return;

    clearGenerationTimers();
    setPrompt("");
    clearError();
    await sendMessage({ text });
  };

  const startNewScenario = () => {
    clearGenerationTimers();
    stop();
    setPrompt("");
    setMessages([]);
    setGenerationByMessage({});
  };

  const failGeneration = (messageId: string, error: string, jobId?: string) => {
    setGenerationByMessage((current) => ({
      ...current,
      [messageId]: {
        ...current[messageId],
        status: "failed",
        error,
        jobId,
        currentStage: "Generation failed",
      },
    }));
  };

  const generate = async (messageId: string, scenario: ScenarioConfiguration) => {
    clearGenerationTimers();
    setGenerationByMessage((current) => ({
      ...current,
      [messageId]: {
        status: "generating",
        progress: 5,
        currentStage: "Submitting generation job",
      },
    }));

    try {
      const res = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario }),
      });

      if (!res.ok) {
        const payload = await res.json().catch(() => undefined) as { detail?: string } | undefined;
        throw new Error(payload?.detail ?? `Generation request failed (${res.status}).`);
      }

      const jobData = await res.json();
      const jobId = jobData.jobId;
      setGenerationByMessage((current) => ({
        ...current,
        [messageId]: {
          status: jobData.status as GenerationStatus,
          jobId,
          progress: jobData.progress,
          currentStage: jobData.currentStage,
        },
      }));

      const interval = window.setInterval(async () => {
        try {
          const pollRes = await fetch(`/api/status/${jobId}`);
          if (!pollRes.ok) {
            failGeneration(messageId, `Status check failed (${pollRes.status}).`, jobId);
            window.clearInterval(interval);
            return;
          }
          const statusData = await pollRes.json();

          if (statusData.status === "failed") {
            failGeneration(messageId, statusData.currentStage || "Generation failed.", jobId);
            window.clearInterval(interval);
            return;
          }

          setGenerationByMessage((current) => ({
            ...current,
            [messageId]: {
              status: statusData.status as GenerationStatus,
              jobId: statusData.jobId,
              downloadUrl: statusData.downloadUrl,
              stats: statusData.stats,
              progress: statusData.progress,
              currentStage: statusData.currentStage,
            },
          }));

          if (statusData.status === "complete") {
            window.clearInterval(interval);
          }
        } catch (e) {
          failGeneration(messageId, e instanceof Error ? e.message : "Status check failed.", jobId);
          window.clearInterval(interval);
        }
      }, 400);

      generationTimers.current.push(interval);
    } catch (err) {
      failGeneration(
        messageId,
        err instanceof Error ? err.message : "The generation service is unavailable.",
      );
    }
  };

  const openExample = (name: string) => {
    clearGenerationTimers();
    const example = promptSuggestions.find((item) =>
      name.toLowerCase().startsWith(item.label.split(" ")[0].toLowerCase()),
    );
    submitPrompt(example?.prompt ?? promptSuggestions[0].prompt);
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
          <SidebarGroup>
            <SidebarGroupLabel>Recent scenarios</SidebarGroupLabel>
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

        </SidebarContent>

        <SidebarFooter className="text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">
          Configure → generate → evaluate
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      <SidebarInset className="h-svh overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b px-4">
          <div className="flex items-center gap-2">
            <SidebarTrigger />
            <Separator className="h-4" orientation="vertical" />
            <div>
              <p className="text-xs font-medium">{latestScenario?.title ?? "New scenario"}</p>
              <p className="hidden text-[10px] text-muted-foreground sm:block">
                {messages.length ? "Draft configuration" : "Describe what you want to generate"}
              </p>
            </div>
          </div>
          <Badge variant="outline">MVP studio</Badge>
        </header>

        {!messages.length ? (
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
              <PromptComposer disabled={isThinking} isLoading={isThinking} onChange={setPrompt} onSubmit={submitPrompt} value={prompt} />
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
                      const scenario = message.parts.find((part) => part.type === "data-scenario")?.data;
                      const generation = generationByMessage[message.id] ?? { status: "idle" as const };
                      const isStreamingThis = status === "streaming" && index === messages.length - 1 && message.role === "assistant";

                      return (
                        <MessageScrollerItem
                          key={message.id}
                          messageId={message.id}
                          scrollAnchor={message.role === "user"}
                        >
                          <Message align={message.role === "user" ? "end" : "start"}>
                            {message.role === "assistant" && (
                              <MessageAvatar className="self-start bg-primary text-primary-foreground">
                                <HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} />
                              </MessageAvatar>
                            )}
                            <MessageContent>
                              <Bubble align={message.role === "user" ? "end" : "start"} className={scenario ? "w-full" : undefined} variant={message.role === "user" ? "secondary" : "ghost"}>
                                <BubbleContent className={scenario ? "w-full" : undefined}>
                                  {text && (
                                    <p className={scenario ? "mb-3 text-xs leading-5" : undefined}>
                                      {text}
                                      {isStreamingThis && (
                                        <span className="ml-1 inline-block h-3.5 w-1.5 animate-pulse rounded-xs bg-primary align-middle" />
                                      )}
                                    </p>
                                  )}
                                  {scenario && (
                                    <ScenarioCard
                                      currentStage={generation.currentStage}
                                      error={generation.error}
                                      jobId={generation.jobId}
                                      onGenerate={(draft) => generate(message.id, draft)}
                                      progress={generation.progress}
                                      scenario={scenario}
                                      stats={generation.stats}
                                      status={generation.status}
                                    />
                                  )}
                                </BubbleContent>
                              </Bubble>
                            </MessageContent>
                          </Message>
                        </MessageScrollerItem>
                      );
                    })}
                    {status === "submitted" && (
                      <MessageScrollerItem messageId="thinking-indicator" scrollAnchor>
                          <Message align="start">
                          <MessageAvatar className="self-start bg-primary text-primary-foreground">
                            <HugeiconsIcon icon={AiMagicIcon} strokeWidth={2} />
                          </MessageAvatar>
                          <MessageContent>
                            <Bubble align="start" className="w-full" variant="ghost">
                              <BubbleContent className="w-full">
                                <div className="flex items-center gap-2 py-1 text-xs text-muted-foreground">
                                  <Spinner />
                                  <span className="shimmer">Syda Agent is analyzing schema & compiling scenario rules…</span>
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
                  <PromptComposer compact disabled={isThinking} isLoading={isThinking} onChange={setPrompt} onSubmit={submitPrompt} value={prompt} />
                  {isThinking && (
                    <Button onClick={() => stop()} type="button" variant="outline">
                      Stop
                    </Button>
                  )}
                </div>
                {error && <p className="mt-2 text-xs text-destructive">{error.message}</p>}
              </div>
            </div>
          </main>
        )}
      </SidebarInset>
    </SidebarProvider>
  );
}
