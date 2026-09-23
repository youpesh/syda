import os
import re
import asyncio
import logging
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from syda.schemas import validate_schema
from app.models.scenario import ScenarioConfiguration, SecondaryMetric, ChatResponse

load_dotenv(override=True)
load_dotenv("backend/.env", override=True)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Syda AI, an expert agent for synthetic data architecture and causal scenario generation.
Your job is to translate user requirements into a structured synthetic data scenario configuration.

A scenario consists of:
1. title: A concise, professional title.
2. description: A clear description of the business process and constraints.
3. record_count: Complete lifecycle instances to generate (e.g., 5000, 10000). Default to 10000 unless specified.
4. secondary_metric: An important domain-specific constraint or ratio (e.g., "Denial rate": "15%", "Fraud rate": "3%", "Care-gap rate": "8%").
5. workflow: Sequential entity names preserving causal order (e.g., ["Patient", "Visit", "Diagnosis", "Treatment", "Follow-up"]).
6. paths: Optional weighted branches with name, included workflow steps, and per-step field overrides.
7. rules: List of temporal, referential, and business constraints (e.g., "Visits occur before diagnoses", "Denied claims cannot be paid").
8. schemas: Native relational table definitions mapping table name to column schemas with types, primary keys, and __foreign_keys__.

Provide a concise, helpful response message explaining the design choices or updates."""

# Domain profiles for fallback and intelligent templating with native syda schemas
DOMAIN_TEMPLATES = [
    {
        "matches": re.compile(r"insurance|claim|denial|policy|adjudicat", re.IGNORECASE),
        "title": "Insurance claims",
        "description": "Healthcare insurance claims with causal adjudication and payment constraints.",
        "record_count": 10000,
        "secondary_metric": {"label": "Denial rate", "value": "15%"},
        "workflow": ["Patient", "Policy", "Diagnosis", "Claim", "Payment"],
        "paths": [
            {
                "name": "approved",
                "steps": ["Patient", "Policy", "Diagnosis", "Claim", "Payment"],
                "weight": 0.75,
                "overrides": {"Claim": {"status": "approved"}},
            },
            {
                "name": "denied",
                "steps": ["Patient", "Policy", "Diagnosis", "Claim"],
                "weight": 0.15,
                "overrides": {"Claim": {"status": "denied"}},
            },
            {
                "name": "pending",
                "steps": ["Patient", "Policy", "Diagnosis", "Claim"],
                "weight": 0.10,
                "overrides": {"Claim": {"status": "pending"}},
            },
        ],
        "rules": [
            "Policy must be active before claim submission",
            "Diagnosis occurs before claim creation",
            "Adjudication precedes payment disbursement",
            "Denied claims cannot be paid",
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
                "diagnosis_id": "foreign_key",
                "claim_amount": {"type": "float", "constraints": {"min": 10.0}},
                "claim_date": "date",
                "status": "text",
                "__foreign_keys__": {
                    "policy_id": "Policy.policy_id",
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


def _heuristic_compile_scenario(prompt: str, current: Optional[ScenarioConfiguration] = None) -> ChatResponse:
    """Intelligent fallback compiler that parses prompt requirements into a structured, validated scenario."""
    if current:
        scenario = current.model_copy(deep=True)
        message = "I've updated your scenario configuration based on your requirements."

        # Check for record count updates
        match_records = re.search(r"(\d[\d,]*)\s*(?:records?|rows?|claims?|orders?)", prompt, re.IGNORECASE)
        if match_records:
            scenario.record_count = int(match_records.group(1).replace(",", ""))

        # Check for rate updates
        match_rate = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:are\s+)?([a-zA-Z\-_ ]+)", prompt, re.IGNORECASE)
        if match_rate:
            pct, label = match_rate.group(1), match_rate.group(2).strip()
            scenario.secondary_metric = SecondaryMetric(label=f"{label.capitalize()} rate", value=f"{pct}%")

        # Check for added rules
        if "rule" in prompt.lower():
            rule_match = re.search(r"(?:add\s+rule|rule:?)\s*(?:that)?\s*[\"']?([^\"'\.\n]+)[\"']?", prompt, re.IGNORECASE)
            if rule_match:
                new_rule = rule_match.group(1).strip()
                if new_rule and new_rule not in scenario.rules:
                    scenario.rules.append(new_rule)
        return ChatResponse(message=message, scenario=scenario)

    # Search for matching domain profile
    selected_template = DEFAULT_TEMPLATE
    for tmpl in DOMAIN_TEMPLATES:
        if tmpl["matches"].search(prompt):
            selected_template = tmpl
            break

    record_count = selected_template["record_count"]
    match_records = re.search(r"(\d[\d,]*)\s*(?:records?|rows?|claims?|orders?|patients?|transactions?)", prompt, re.IGNORECASE)
    if match_records:
        record_count = int(match_records.group(1).replace(",", ""))

    sec_metric = SecondaryMetric(
        label=selected_template["secondary_metric"]["label"],
        value=selected_template["secondary_metric"]["value"],
    )
    match_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", prompt)
    if match_pct:
        sec_metric.value = f"{match_pct.group(1)}%"

    # Validate template schemas using syda core validator
    raw_schemas = selected_template.get("schemas", {})
    validated_schemas = _validate_and_sanitize_schemas(raw_schemas)

    scenario = ScenarioConfiguration(
        title=selected_template["title"],
        description=selected_template["description"],
        record_count=record_count,
        secondary_metric=sec_metric,
        workflow=list(selected_template["workflow"]),
        paths=list(selected_template.get("paths", [])),
        rules=list(selected_template["rules"]),
        schemas=validated_schemas,
    )

    return ChatResponse(
        message=f"I’ve created a structured scenario for {scenario.title.lower()} with {scenario.record_count:,} lifecycle instances across {len(scenario.workflow)} workflow steps and verified relational schemas. Review the configuration below before generating.",
        scenario=scenario,
    )


async def compile_scenario_agent(
    prompt: str,
    current_scenario: Optional[ScenarioConfiguration] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> ChatResponse:
    """
    Translates natural language prompt into a structured ScenarioConfiguration.
    Attempts to use Pydantic AI if an active LLM provider key is available (with a fast 10s timeout);
    otherwise smoothly falls back to the native heuristic compiler.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if openai_key or gemini_key or anthropic_key:
        try:
            from pydantic_ai import Agent

            model_name = (
                "openai:gpt-4o-mini" if openai_key
                else ("anthropic:claude-3-5-sonnet-latest" if anthropic_key
                else "google:gemini-3.6-flash")
            )
            agent = Agent(
                model_name,
                output_type=ChatResponse,
                system_prompt=SYSTEM_PROMPT,
            )

            context_str = f"User Request: {prompt}\n"
            if current_scenario:
                context_str += f"\nExisting Scenario Configuration:\n{current_scenario.model_dump_json(indent=2)}\n"

            # 10 second timeout prevents upstream 503 retry storms from hanging the client
            result = await asyncio.wait_for(agent.run(context_str), timeout=10.0)
            output = getattr(result, "output", getattr(result, "data", None))
            fallback = _heuristic_compile_scenario(prompt, current_scenario)
            if output and isinstance(output, ChatResponse):
                output.scenario.schemas = _validate_and_sanitize_schemas(output.scenario.schemas, fallback.scenario.schemas)
                return output
            elif output:
                res = ChatResponse.model_validate(output)
                res.scenario.schemas = _validate_and_sanitize_schemas(res.scenario.schemas, fallback.scenario.schemas)
                return res
        except Exception as e:
            logger.warning(f"Pydantic AI agent call failed ({e}). Falling back to native heuristic compiler.")

    return _heuristic_compile_scenario(prompt, current_scenario)
