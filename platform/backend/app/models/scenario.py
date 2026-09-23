from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any, Literal

class SecondaryMetric(BaseModel):
    label: str
    value: str


class ScenarioPathConfiguration(BaseModel):
    name: str = Field(description="Stable name for a normal, edge, or failure path")
    steps: List[str] = Field(description="Workflow steps included in this path")
    weight: float = Field(default=1.0, gt=0, description="Relative batch frequency")
    overrides: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-step field values enforced for this path",
    )


class ScenarioConfiguration(BaseModel):
    title: str = Field(description="Title of the synthetic data scenario")
    description: str = Field(description="Summary description of what the dataset models")
    record_count: int = Field(
        default=10000,
        alias="recordCount",
        ge=1,
        le=1_000_000,
        description="Number of complete scenario instances to generate",
    )
    secondary_metric: SecondaryMetric = Field(alias="secondaryMetric", description="Key metric such as denial rate or table count")
    workflow: List[str] = Field(description="Sequential entities in causal order (e.g. ['Patient', 'Visit', 'Claim'])")
    paths: List[ScenarioPathConfiguration] = Field(
        default_factory=list,
        description="Optional weighted workflow branches; an empty list uses the full workflow",
    )
    rules: List[str] = Field(description="Causal, temporal, and referential rules to enforce")
    schemas: Optional[Dict[str, Any]] = Field(default=None, description="Native syda table schema definitions with types and foreign keys")

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ChatRequest(BaseModel):
    prompt: str = Field(..., description="User requirements or prompt for synthetic data")
    current_scenario: Optional[ScenarioConfiguration] = Field(None, alias="currentScenario", description="Current scenario configuration if refining an existing one")
    history: Optional[List[Dict[str, Any]]] = Field(default=[], description="Previous conversation turns")

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

class ChatResponse(BaseModel):
    message: str = Field(description="Conversational explanation of the scenario generated or modified")
    scenario: ScenarioConfiguration = Field(description="Structured scenario configuration")

class GenerateRequest(BaseModel):
    scenario: ScenarioConfiguration

class EvaluationMetric(BaseModel):
    key: str
    label: str
    status: Literal["pass", "fail", "not_evaluated"] = Field(alias="status")
    value: str
    detail: str
    violations: int = 0

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class JobStats(BaseModel):
    causal_integrity: str = Field(alias="causalIntegrity")
    referential_integrity: str = Field(alias="referentialIntegrity")
    compliance: str
    records_generated: int = Field(alias="recordsGenerated")
    flagged_records: int = Field(alias="flaggedRecords")
    duration_seconds: float = Field(alias="durationSeconds")
    table_row_counts: Dict[str, int] = Field(default_factory=dict, alias="tableRowCounts")
    path_counts: Dict[str, int] = Field(default_factory=dict, alias="pathCounts")
    evaluation_metrics: List[EvaluationMetric] = Field(default_factory=list, alias="evaluationMetrics")
    evaluation_result: Literal["pass", "fail", "partial"] = Field(default="partial", alias="evaluationResult")

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )


class CostEstimate(BaseModel):
    provider: str
    model: str
    estimated_input_tokens: int = Field(alias="estimatedInputTokens")
    estimated_output_tokens: int = Field(alias="estimatedOutputTokens")
    estimated_cost_usd: Optional[float] = Field(alias="estimatedCostUsd")
    note: str

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

class JobStatusResponse(BaseModel):
    job_id: str = Field(alias="jobId")
    status: str  # "generating" | "validating" | "evaluating" | "complete"
    progress: int  # 0 to 100
    current_stage: str = Field(alias="currentStage")
    stats: Optional[JobStats] = None
    download_url: Optional[str] = Field(None, alias="downloadUrl")

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )
