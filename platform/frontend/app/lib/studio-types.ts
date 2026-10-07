import type { UIMessage } from "ai";

// Frontend DTOs for backend/app/models/scenario.py, entities.py, and API previews.
// Table schemas intentionally allow extensible Syda field and metadata definitions.
export type SchemaTable = Record<string, unknown>;

export interface ScenarioConfiguration {
  title: string;
  description: string;
  recordCount: number;
  secondaryMetric: { label: string; value: string };
  workflow: string[];
  rules: string[];
  schemas?: Record<string, SchemaTable> | null;
  paths?: { name: string; steps: string[]; weight: number; overrides: Record<string, Record<string, unknown>> }[];
  checks?: Record<string, unknown>[];
}

export interface CostEstimate {
  provider: string;
  model: string;
  estimatedInputTokens: number;
  estimatedOutputTokens: number;
  estimatedCostUsd: number | null;
  note: string;
}

export interface JobStats {
  causalIntegrity: string;
  referentialIntegrity: string;
  compliance: string;
  recordsGenerated: number;
  flaggedRecords: number;
  durationSeconds: number;
  tableRowCounts: Record<string, number>;
  pathCounts: Record<string, number>;
  evaluationResult: "pass" | "fail" | "partial";
  evaluationMetrics: { key: string; label: string; status: "pass" | "fail" | "not_evaluated"; value: string; detail: string; violations: number }[];
}

export interface GenerationJob {
  jobId: string;
  scenarioId?: string | null;
  status: string;
  progress: number;
  currentStage: string;
  scenario?: ScenarioConfiguration;
  stats?: JobStats;
  downloadUrl?: string | null;
  filesAvailable?: boolean;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface DatasetPreview {
  jobId: string;
  tables: Record<string, { columns: string[]; rows: Record<string, unknown>[]; rowCount: number }>;
}

export interface SavedScenario {
  id: string;
  title: string;
  description: string;
  recordCount: number;
  secondaryMetric: ScenarioConfiguration["secondaryMetric"] | null;
  workflow: string[];
  rules: string[];
  configuration: ScenarioConfiguration;
  createdAt: string;
  updatedAt: string;
}

export interface ChatRunReference {
  messageId: string;
  jobId: string;
  scenarioId: string;
  scenario: ScenarioConfiguration;
}

export interface SavedConversationSummary {
  id: string;
  title: string;
  createdAt: string | null;
  updatedAt: string | null;
  messageCount: number;
}

export interface SavedConversation extends SavedConversationSummary {
  messages: UIMessage<unknown, { scenario: ScenarioConfiguration }>[];
  runs: ChatRunReference[];
  scenarioDraft?: ScenarioConfiguration | null;
  draftVersion: number;
}
