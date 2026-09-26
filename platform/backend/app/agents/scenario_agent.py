import re
import asyncio
import logging
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from syda.schemas import validate_schema
from app.models.scenario import ScenarioConfiguration, ScenarioPathConfiguration, ScenarioCheck, SecondaryMetric, ChatResponse
from app.provider_settings import resolve_provider

load_dotenv(override=True)
load_dotenv("backend/.env", override=True)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Syda AI, an expert agent for synthetic data architecture and causal scenario generation.
Your job is to help the user define a useful synthetic data scenario through conversation.

Conversation behavior:
- Reply naturally to greetings and general questions. Do not create a scenario for a greeting or an unrelated question.
- If the user has not provided enough information to identify the data domain or core entities, ask a focused clarifying question and return scenario=null.
- Create or update a scenario only when the conversation contains enough detail to make a useful draft. A useful draft has at least two ordered workflow steps, meaningful rules, and relational schemas.
- Treat the conversation history and the existing scenario as context. For a follow-up, preserve existing requirements unless the user asks to change them, and explain what changed.
- Treat the existing scenario supplied in context as canonical; do not fetch it again.
- For a new scenario, call create_scenario_draft with only the fields that need to differ from its domain starter. The tool starts from the closest validated domain template and keeps its table schemas; do not try to emit or rewrite schemas during initial creation. For an existing scenario, call update_scenario_draft with only the fields that should change; the tool merges the patch into the saved draft and validates the complete result.
- If a draft tool returns validation errors, correct the listed fields and call the tool again. Do not treat draft validation errors as a provider failure.
- Never infer a specific value from vague language such as “smaller” or “faster.” Ask a concise question when the requested change is ambiguous. Only confirm a change after the draft tool succeeds, and use its returned title, table count, and instance count.
- Chat can design and validate a dataset scenario; it must not start dataset generation. Generation requires the user's explicit action in the interface.

A scenario consists of:
1. title: A concise, professional title.
2. description: A clear description of the business process and constraints.
3. record_count: Complete lifecycle instances to generate (e.g., 5000, 10000). Default to 10000 unless specified.
4. secondary_metric: An important domain-specific constraint or ratio (e.g., "Denial rate": "15%", "Fraud rate": "3%", "Care-gap rate": "8%").
5. workflow: Sequential entity names preserving causal order (e.g., ["Patient", "Visit", "Diagnosis", "Treatment", "Follow-up"]).
6. paths: Optional weighted branches with name, included workflow steps, and per-step field overrides.
7. rules: List of temporal, referential, and business constraints (e.g., "Visits occur before diagnoses", "Denied claims cannot be paid").
8. schemas: Native relational table definitions mapping table name to column schemas with types, primary keys, and __foreign_keys__.
9. checks: Machine-checkable rules. Use kind='allowed_values' with table, field, and values; kind='temporal_order' with before and after Table.field references; or kind='absence_by_value' with source='Table.field', value, and absent_table to prohibit a later table for matching instances. Encode target rates in path weights/overrides when applicable. Keep checks consistent with workflow paths and schemas. Do not claim a free-text rule is enforced unless it has a matching check or schema/path constraint.

Provide a concise, helpful response message explaining the design choices or updates."""


class ScenarioDraftPatch(BaseModel):
    """A partial scenario update. Unspecified fields stay as they are."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    title: Optional[str] = None
    description: Optional[str] = None
    record_count: Optional[int] = Field(default=None, alias="recordCount", ge=1, le=1_000_000)
    secondary_metric: Optional[SecondaryMetric] = Field(default=None, alias="secondaryMetric")
    workflow: Optional[List[str]] = None
    paths: Optional[List[ScenarioPathConfiguration]] = None
    rules: Optional[List[str]] = None
    checks: Optional[List[ScenarioCheck]] = None
    schemas: Optional[Dict[str, Dict[str, Any]]] = None


class ScenarioCreationPatch(BaseModel):
    """Small initial-draft patch; validated starter schemas stay canonical."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    title: Optional[str] = None
    description: Optional[str] = None
    record_count: Optional[int] = Field(default=None, alias="recordCount", ge=1, le=1_000_000)
    secondary_metric: Optional[SecondaryMetric] = Field(default=None, alias="secondaryMetric")
    workflow: Optional[List[str]] = None
    paths: Optional[List[ScenarioPathConfiguration]] = None
    rules: Optional[List[str]] = None
    checks: Optional[List[ScenarioCheck]] = None


class ScenarioAgentReply(BaseModel):
    """Small final conversational response; scenario state is returned by tools."""

    message: str


def _run_usage_counts(result: Any) -> tuple[int | None, int | None]:
    usage_method = getattr(result, "usage", None)
    usage = usage_method() if callable(usage_method) else usage_method
    return getattr(usage, "requests", None), getattr(usage, "tool_calls", None)

# Domain profiles for fallback and intelligent templating with native syda schemas
DOMAIN_TEMPLATES = [
    {
        "matches": re.compile(r"insurance|claim|denial|policy|adjudicat", re.IGNORECASE),
        "title": "Insurance claims",
        "description": "Healthcare insurance claims with causal adjudication and payment constraints.",
        "record_count": 10000,
        "secondary_metric": {"label": "Denial rate", "value": "15%"},
        "workflow": ["Patient", "Policy", "Provider", "Diagnosis", "Claim", "Payment"],
        "paths": [
            {
                "name": "approved",
                "steps": ["Patient", "Policy", "Provider", "Diagnosis", "Claim", "Payment"],
                "weight": 0.75,
                "overrides": {"Claim": {"status": "approved"}},
            },
            {
                "name": "denied",
                "steps": ["Patient", "Policy", "Provider", "Diagnosis", "Claim"],
                "weight": 0.15,
                "overrides": {"Claim": {"status": "denied"}},
            },
            {
                "name": "pending",
                "steps": ["Patient", "Policy", "Provider", "Diagnosis", "Claim"],
                "weight": 0.10,
                "overrides": {"Claim": {"status": "pending"}},
            },
        ],
        "rules": [
            "Policy coverage starts before claim submission",
            "Each claim references a provider with a specialty and network status",
            "Diagnosis occurs before claim creation",
            "Adjudication precedes payment disbursement",
            "Denied claims cannot be paid",
        ],
        "checks": [
            {"name": "claim_status_values", "kind": "allowed_values", "table": "Claim", "field": "status", "values": ["approved", "denied", "pending"]},
            {"name": "provider_network_status_values", "kind": "allowed_values", "table": "Provider", "field": "network_status", "values": ["in_network", "out_of_network"]},
            {"name": "denied_claims_have_no_payment", "kind": "absence_by_value", "source": "Claim.status", "value": "denied", "absent_table": "Payment"},
            {"name": "policy_active_before_claim", "kind": "temporal_order", "before": "Policy.start_date", "after": "Claim.claim_date"},
            {"name": "diagnosis_before_claim", "kind": "temporal_order", "before": "Diagnosis.diagnosis_date", "after": "Claim.claim_date"},
            {"name": "claim_before_payment", "kind": "temporal_order", "before": "Claim.claim_date", "after": "Payment.payment_date"},
        ],
        "schemas": {
            "Patient": {
                "patient_id": {"type": "integer", "constraints": {"primary_key": True}},
                "name": "text",
                "dob": "date",
                "gender": "text",
            },
            "Policy": {
                "policy_id": {"type": "integer", "constraints": {"primary_key": True}},
                "patient_id": "foreign_key",
                "policy_type": "text",
                "start_date": "date",
                "__foreign_keys__": {"patient_id": "Patient.patient_id"},
            },
            "Provider": {
                "provider_id": {"type": "integer", "constraints": {"primary_key": True}},
                "provider_name": "text",
                "specialty": "text",
                "network_status": "text",
            },
            "Diagnosis": {
                "diagnosis_id": {"type": "integer", "constraints": {"primary_key": True}},
                "patient_id": "foreign_key",
                "icd10": "text",
                "diagnosis_date": "date",
                "__foreign_keys__": {"patient_id": "Patient.patient_id"},
            },
            "Claim": {
                "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
                "policy_id": "foreign_key",
                "provider_id": "foreign_key",
                "diagnosis_id": "foreign_key",
                "claim_amount": {"type": "float", "constraints": {"min": 10.0}},
                "claim_date": "date",
                "status": "text",
                "__foreign_keys__": {
                    "policy_id": "Policy.policy_id",
                    "provider_id": "Provider.provider_id",
                    "diagnosis_id": "Diagnosis.diagnosis_id",
                },
            },
            "Payment": {
                "payment_id": {"type": "integer", "constraints": {"primary_key": True}},
                "claim_id": "foreign_key",
                "paid_amount": {"type": "float", "constraints": {"min": 0.0}},
                "payment_date": "date",
                "__foreign_keys__": {"claim_id": "Claim.claim_id"},
            },
        },
    },
    {
        "matches": re.compile(r"e-?commerce|commerce|retail|order|cart|product|checkout|refund", re.IGNORECASE),
        "title": "E-commerce orders",
        "description": "Multi-table retail transaction lifecycle spanning customer orders, payments, and refunds.",
        "record_count": 25000,
        "secondary_metric": {"label": "Tables", "value": "5"},
        "workflow": ["Customer", "Product", "Order", "Payment", "Refund"],
        "rules": [
            "Orders contain valid catalog products",
            "Payment authorization must succeed prior to fulfillment",
            "Refund amounts cannot exceed original payment",
            "Delivery timestamps occur after order creation",
        ],
        "schemas": {
            "Customer": {
                "customer_id": {"type": "integer", "constraints": {"primary_key": True}},
                "name": "text",
                "email": "email",
                "created_at": "date",
            },
            "Product": {
                "product_id": {"type": "integer", "constraints": {"primary_key": True}},
                "sku": "text",
                "unit_price": {"type": "float", "constraints": {"min": 1.0}},
            },
            "Order": {
                "order_id": {"type": "integer", "constraints": {"primary_key": True}},
                "customer_id": "foreign_key",
                "order_date": "date",
                "total_amount": {"type": "float", "constraints": {"min": 0.0}},
                "__foreign_keys__": {"customer_id": "Customer.customer_id"},
            },
            "Payment": {
                "payment_id": {"type": "integer", "constraints": {"primary_key": True}},
                "order_id": "foreign_key",
                "amount": {"type": "float", "constraints": {"min": 0.0}},
                "payment_method": "text",
                "__foreign_keys__": {"order_id": "Order.order_id"},
            },
            "Refund": {
                "refund_id": {"type": "integer", "constraints": {"primary_key": True}},
                "payment_id": "foreign_key",
                "refund_amount": {"type": "float", "constraints": {"min": 0.0}},
                "reason": "text",
                "__foreign_keys__": {"payment_id": "Payment.payment_id"},
            },
        },
    },
    {
        "matches": re.compile(r"patient|journey|clinical|hospital|health|care gap", re.IGNORECASE),
        "title": "Patient care journeys",
        "description": "Longitudinal patient clinical care pathways with temporal event ordering.",
        "record_count": 5000,
        "secondary_metric": {"label": "Care-gap rate", "value": "8%"},
        "workflow": ["Patient", "Encounter", "Diagnosis", "TreatmentPlan"],
        "rules": [
            "Encounters precede diagnoses and orders",
            "Treatments require a qualifying diagnosis",
            "Lab results must be recorded within encounter timeframe",
            "Follow-ups occur strictly after discharge or treatment",
        ],
        "schemas": {
            "Patient": {
                "patient_id": {"type": "integer", "constraints": {"primary_key": True}},
                "name": "text",
                "birth_date": "date",
                "gender": "text",
            },
            "Encounter": {
                "encounter_id": {"type": "integer", "constraints": {"primary_key": True}},
                "patient_id": "foreign_key",
                "encounter_type": "text",
                "admit_date": "date",
                "discharge_date": "date",
                "__foreign_keys__": {"patient_id": "Patient.patient_id"},
            },
            "Diagnosis": {
                "diagnosis_id": {"type": "integer", "constraints": {"primary_key": True}},
                "encounter_id": "foreign_key",
                "icd10": "text",
                "__foreign_keys__": {"encounter_id": "Encounter.encounter_id"},
            },
            "TreatmentPlan": {
                "plan_id": {"type": "integer", "constraints": {"primary_key": True}},
                "diagnosis_id": "foreign_key",
                "regimen": "text",
                "start_date": "date",
                "__foreign_keys__": {"diagnosis_id": "Diagnosis.diagnosis_id"},
            },
        },
    },
    {
        "matches": re.compile(r"fraud|bank|finance|transaction|credit|loan|account", re.IGNORECASE),
        "title": "Financial transactions & fraud",
        "description": "Banking transactions with anti-money laundering and fraudulent pattern signals.",
        "record_count": 50000,
        "secondary_metric": {"label": "Fraud rate", "value": "2.5%"},
        "workflow": ["Account", "Transaction", "FraudAlert"],
        "rules": [
            "Account must be KYC verified before transactional activity",
            "Transactions flagged as high-risk trigger fraud investigation",
            "Blocked transactions are not settled",
            "Transaction timestamp must fall within account active period",
        ],
        "schemas": {
            "Account": {
                "account_id": {"type": "integer", "constraints": {"primary_key": True}},
                "account_number": "text",
                "account_type": "text",
                "balance": {"type": "float", "constraints": {"min": 0.0}},
            },
            "Transaction": {
                "transaction_id": {"type": "integer", "constraints": {"primary_key": True}},
                "account_id": "foreign_key",
                "amount": {"type": "float"},
                "timestamp": "datetime",
                "merchant": "text",
                "__foreign_keys__": {"account_id": "Account.account_id"},
            },
            "FraudAlert": {
                "alert_id": {"type": "integer", "constraints": {"primary_key": True}},
                "transaction_id": "foreign_key",
                "risk_score": {"type": "float", "constraints": {"min": 0.0, "max": 1.0}},
                "action_taken": "text",
                "__foreign_keys__": {"transaction_id": "Transaction.transaction_id"},
            },
        },
    },
]

DEFAULT_TEMPLATE = {
    "title": "Custom synthetic scenario",
    "description": "Multi-table relational synthetic data scenario with causal and referential integrity.",
    "record_count": 10000,
    "secondary_metric": {"label": "Rules", "value": "4"},
    "workflow": ["Entity", "Event", "Outcome"],
    "rules": [
        "Events preserve declared temporal order",
        "Foreign key relationships maintain 100% referential integrity",
        "Outcome states obey legal business transition rules",
    ],
    "schemas": {
        "Entity": {
            "entity_id": {"type": "integer", "constraints": {"primary_key": True}},
            "name": "text",
            "status": "text",
        },
        "Event": {
            "event_id": {"type": "integer", "constraints": {"primary_key": True}},
            "entity_id": "foreign_key",
            "event_type": "text",
            "timestamp": "datetime",
            "__foreign_keys__": {"entity_id": "Entity.entity_id"},
        },
        "Outcome": {
            "outcome_id": {"type": "integer", "constraints": {"primary_key": True}},
            "event_id": "foreign_key",
            "result": "text",
            "__foreign_keys__": {"event_id": "Event.event_id"},
        },
    },
}


def _starter_scenario_for_context(context: str) -> ScenarioConfiguration:
    """Return the closest validated starter so the model only has to describe changes."""
    template = next(
        (item for item in DOMAIN_TEMPLATES if item["matches"].search(context)),
        DEFAULT_TEMPLATE,
    )
    data = {key: value for key, value in template.items() if key != "matches"}
    return ScenarioConfiguration.model_validate(data)


def _has_concrete_scenario_subject(prompt: str) -> bool:
    return any(template["matches"].search(prompt) for template in DOMAIN_TEMPLATES) or bool(
        re.search(
            r"\b(patient|member|customer|user|account|policy|claim|visit|encounter|diagnosis|treatment|"
            r"order|product|payment|refund|transaction|shipment|appointment|loan|employee|device)\b",
            prompt,
            re.IGNORECASE,
        )
    )


def _has_scenario_change_intent(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(add|change|update|modify|remove|delete|include|exclude|increase|decrease|set|make it|"
            r"instead|also|revise|rename|replace|generate|generating|create|creating|design|designing|"
            r"build|building|model|modeling|need|want|plan|planning|produce|prepare|make|reduce|shrink|scale\s+down|"
            r"smaller|faster|quicker|fewer|less\s+data)\b",
            prompt,
            re.IGNORECASE,
        )
        or _record_count_match(prompt)
        or re.search(r"\b\d+(?:\.\d+)?\s*%", prompt)
    )


def _record_count_match(prompt: str) -> re.Match[str] | None:
    return re.search(
        r"\b(\d[\d,]*)\s*(?:(?:lifecycle\s+)?instances?|records?|rows?|claims?|orders?|patients?|transactions?|samples?)\b",
        prompt,
        re.IGNORECASE,
    )


def _is_initial_scenario_request(prompt: str) -> bool:
    return _has_concrete_scenario_subject(prompt) and _has_scenario_change_intent(prompt)


def _has_scenario_recall_intent(prompt: str) -> bool:
    return bool(
        re.search(r"\b(show|display|review|summari[sz]e|open|pull\s+up)\b", prompt, re.IGNORECASE)
        and re.search(r"\b(draft|scenario|plan|configuration)\b", prompt, re.IGNORECASE)
    )


def _scenario_is_useful(scenario: ScenarioConfiguration) -> bool:
    return (
        len(scenario.workflow) >= 2
        and bool(scenario.rules)
        and bool(scenario.schemas)
        and all(isinstance(table, dict) and bool(table) for table in scenario.schemas.values())
    )


def _validate_scenario_candidate(
    candidate: ScenarioConfiguration,
) -> tuple[Optional[ScenarioConfiguration], list[str]]:
    errors: list[str] = []
    if not _scenario_is_useful(candidate):
        errors.append("A useful scenario needs at least two workflow steps, rules, and nonempty relational schemas.")
    normalized_schemas: dict[str, Any] = {}
    for table_name, schema in (candidate.schemas or {}).items():
        if not isinstance(schema, dict):
            errors.append(f"{table_name}: schema must be an object mapping field names to definitions.")
            continue
        try:
            normalized_schemas[table_name] = validate_schema(schema)
        except Exception as error:
            errors.append(f"{table_name}: {error}")
    for check in candidate.checks:
        try:
            check.validate_references(normalized_schemas)
        except ValueError as error:
            errors.append(f"{check.name}: {error}")
    if errors:
        return None, errors
    return candidate.model_copy(update={"schemas": normalized_schemas}), []


def _validate_and_sanitize_schemas(schemas: Optional[Dict[str, Any]], fallback_schemas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validates each table schema using syda.schemas.validate_schema and ensures valid dictionary structure."""
    if fallback_schemas is None:
        fallback_schemas = DEFAULT_TEMPLATE["schemas"]

    if not schemas or not isinstance(schemas, dict):
        return fallback_schemas

    validated = {}
    for table_name, table_def in schemas.items():
        if isinstance(table_def, dict):
            try:
                validated[table_name] = validate_schema(table_def)
            except Exception as e:
                logger.warning(f"Schema validation error on table {table_name}: {e}. Preserving raw definition.")
                validated[table_name] = table_def

    return validated if validated else fallback_schemas


def _heuristic_compile_scenario(
    prompt: str,
    current: Optional[ScenarioConfiguration] = None,
    conversation_context: Optional[str] = None,
) -> ChatResponse:
    """Return safe conversational fallbacks without pretending an edit was applied."""
    if current:
        if _has_scenario_recall_intent(prompt):
            return ChatResponse(
                message=f"Here’s the current draft for {current.title}. Review it below; choose Generate dataset when you’re ready to start generation.",
                scenario=current,
            )
        if not _has_scenario_change_intent(prompt):
            return ChatResponse(
                message="I’m here to help refine this scenario. What would you like to change or understand?",
                scenario=None,
            )
        return ChatResponse(
            message="I couldn’t update the saved draft because the scenario agent is unavailable. The draft is unchanged; please try again when the configured model is available.",
            scenario=current if _scenario_is_useful(current) else None,
        )

    context = conversation_context or prompt
    if not _is_initial_scenario_request(context):
        return ChatResponse(
            message="Hi! What kind of data are you trying to model? Tell me the process or main entities involved, and I’ll help shape a scenario.",
            scenario=None,
        )

    return ChatResponse(
        message="I couldn’t create a scenario because the configured scenario agent is unavailable. Please check the model connection and try again.",
        scenario=None,
    )


async def compile_scenario_agent(
    prompt: str,
    current_scenario: Optional[ScenarioConfiguration] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    provider_selection: Optional[dict[str, Any]] = None,
) -> ChatResponse:
    """
    Uses the configured conversational agent and validated draft tools. Safe fallback replies
    preserve the canonical draft and never synthesize an edit when the provider is unavailable.
    """
    provider = provider_selection or resolve_provider()
    recent_user_context = "\n".join(
        item["content"]
        for item in (history or [])[-12:]
        if item.get("role") == "user" and isinstance(item.get("content"), str) and item["content"].strip()
    )
    conversation_context = "\n".join(part for part in (recent_user_context, prompt) if part)
    should_attach_scenario = (
        _has_scenario_change_intent(prompt)
        if current_scenario is not None
        else _is_initial_scenario_request(conversation_context)
    )

    if provider["agent_model"]:
        try:
            from syda.llm import LLMClient
            from syda.schemas import ModelConfig
            from pydantic_ai import UsageLimits

            agent = LLMClient(ModelConfig(
                provider=provider["id"],
                model_name=provider["model"],
                extra_kwargs={"api_key": provider["key"], **({"base_url": provider["base_url"]} if provider.get("base_url") else {})},
            )).create_agent(ScenarioAgentReply, system_prompt=SYSTEM_PROMPT, retries=1)

            context_str = f"User Request: {prompt}\n"
            if history:
                context_str += "\nRecent Conversation:\n"
                context_str += "\n".join(
                    f"{item.get('role', 'user').capitalize()}: {item.get('content', '')}"
                    for item in history[-12:]
                    if isinstance(item.get("content"), str) and item["content"].strip()
                )
            if current_scenario:
                context_str += f"\nExisting Scenario Configuration:\n{current_scenario.model_dump_json(indent=2)}\n"
            elif should_attach_scenario:
                starter = _starter_scenario_for_context(conversation_context)
                context_str += (
                    "\nValidated domain starter (preserve its relational schema and executable checks "
                    "unless the user asks to change them):\n"
                    f"{starter.model_dump_json(indent=2)}\n"
                )

            # The model decides which fields to change; tools merge and validate
            # that intent against the canonical draft loaded for this turn.
            staged_candidate: dict[str, Optional[ScenarioConfiguration]] = {"scenario": None}

            @agent.tool_plain
            def create_scenario_draft(patch: ScenarioCreationPatch) -> dict[str, Any]:
                """Apply requested changes to a validated domain starter, then validate and stage the new draft."""
                candidate_data = _starter_scenario_for_context(conversation_context).model_dump(by_alias=True)
                changes = patch.model_dump(by_alias=True, exclude_unset=True)
                candidate_data.update(changes)
                try:
                    candidate = ScenarioConfiguration.model_validate(candidate_data)
                except Exception as error:
                    return {"valid": False, "errors": [str(error)]}
                validated, errors = _validate_scenario_candidate(candidate)
                if validated is None:
                    return {"valid": False, "errors": errors}
                staged_candidate["scenario"] = validated
                return {
                    "valid": True,
                    "title": validated.title,
                    "table_count": len(validated.schemas or {}),
                    "record_count": validated.record_count,
                }

            @agent.tool_plain
            def update_scenario_draft(patch: ScenarioDraftPatch) -> dict[str, Any]:
                """Apply only the model-selected fields to the saved draft, preserving all unspecified values."""
                if current_scenario is None:
                    return {"valid": False, "errors": ["There is no saved scenario to update. Create a new draft instead."]}
                changes = patch.model_dump(by_alias=True, exclude_unset=True)
                if not changes:
                    return {"valid": False, "errors": ["The update patch is empty."]}
                merged = current_scenario.model_dump(by_alias=True)
                merged.update(changes)
                try:
                    candidate = ScenarioConfiguration.model_validate(merged)
                except Exception as error:
                    return {"valid": False, "errors": [str(error)]}
                validated, errors = _validate_scenario_candidate(candidate)
                if validated is None:
                    return {"valid": False, "errors": errors}
                staged_candidate["scenario"] = validated
                return {
                    "valid": True,
                    "changed_fields": sorted(changes.keys()),
                    "title": validated.title,
                    "table_count": len(validated.schemas or {}),
                    "record_count": validated.record_count,
                }

            result = await asyncio.wait_for(
                agent.run(context_str, usage_limits=UsageLimits(request_limit=4, tool_calls_limit=3)),
                timeout=30.0,
            )
            request_count, tool_call_count = _run_usage_counts(result)
            logger.info(
                "Scenario agent turn completed (requests=%s, tool_calls=%s)",
                request_count,
                tool_call_count,
            )
            output = getattr(result, "output", getattr(result, "data", None))
            if output:
                reply = (
                    output if isinstance(output, ScenarioAgentReply)
                    else ScenarioAgentReply.model_validate(output)
                )
                if not should_attach_scenario:
                    return ChatResponse(message=reply.message, scenario=None)
                validated_output = staged_candidate["scenario"]
                if validated_output is None:
                    return ChatResponse(
                        message=(
                            "I haven’t changed the saved draft yet. What specific value or scenario change should I apply?"
                            if current_scenario
                            else reply.message
                        ),
                        scenario=current_scenario if current_scenario and _scenario_is_useful(current_scenario) else None,
                    )
                return ChatResponse(message=reply.message, scenario=validated_output)
        except Exception as e:
            response = getattr(e, "response", None)
            status_code = getattr(e, "status_code", None) or getattr(response, "status_code", None)
            logger.warning(
                "Pydantic AI agent call failed (error_type=%s, status_code=%s); no scenario changes were applied.",
                type(e).__name__,
                status_code,
            )

    return _heuristic_compile_scenario(prompt, current_scenario, conversation_context)
