import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from app.agents.scenario_agent import (
    DOMAIN_TEMPLATES,
    ScenarioAgentReply,
    ScenarioCreationPatch,
    compile_scenario_agent,
)
from app.models.scenario import ScenarioConfiguration


def test_creation_schema_input_preserves_nested_json_and_native_compatibility():
    schemas = {"Patient": {"patient_id": {"type": "integer", "constraints": {"primary_key": True}}}}
    for supplied in (schemas, json.dumps(schemas)):
        patch = ScenarioCreationPatch(schemas=supplied)
        assert patch.model_dump(exclude_unset=True)["schemas"] == schemas
    advertised = ScenarioCreationPatch.model_json_schema()["properties"]["schemas"]
    assert any(branch.get("type") == "string" for branch in advertised["anyOf"])
    assert not any(branch.get("type") == "object" for branch in advertised["anyOf"])


class _ScenarioToolTestModel(TestModel):
    def __init__(self, *, tool_name: str, tool_args: dict, final_message: str):
        super().__init__(
            call_tools=[tool_name],
            custom_output_args={"message": final_message},
        )
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.request_count = 0

    async def request(self, *args, **kwargs):
        self.request_count += 1
        return await super().request(*args, **kwargs)

    def gen_tool_args(self, tool_def):
        if tool_def.name == self.tool_name:
            return self.tool_args
        return super().gen_tool_args(tool_def)


def _fake_llm(monkeypatch, model):
    import syda.llm

    class FakeLLMClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_agent(self, output_type, system_prompt="", retries=3):
            assert output_type is ScenarioAgentReply
            return Agent(model, output_type=output_type, system_prompt=system_prompt, retries=retries)

    monkeypatch.setattr(syda.llm, "LLMClient", FakeLLMClient)


def test_agent_creates_valid_scenario_with_one_tool_call(monkeypatch):
    payload = {**DOMAIN_TEMPLATES[0], "record_count": 20}
    candidate = ScenarioConfiguration.model_validate(payload)
    model = _ScenarioToolTestModel(
        tool_name="create_scenario_draft",
        tool_args=candidate.model_dump(by_alias=True),
        final_message="I drafted an insurance claims scenario with five related tables.",
    )
    _fake_llm(monkeypatch, model)

    response = asyncio.run(compile_scenario_agent(
        prompt="Generate 20 healthcare insurance claims",
        provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
    ))

    assert response.message.startswith("I drafted")
    assert response.scenario is not None
    assert response.scenario.record_count == 20
    assert response.scenario.checks
    assert model.request_count == 2
    assert all(
        tool.name != "get_current_scenario"
        for tool in model.last_model_request_parameters.function_tools
    )


def test_agent_updates_only_requested_field_and_preserves_checks(monkeypatch):
    current = ScenarioConfiguration.model_validate(DOMAIN_TEMPLATES[0])
    model = _ScenarioToolTestModel(
        tool_name="update_scenario_draft",
        tool_args={"recordCount": 250},
        final_message="I increased the scenario to 250 lifecycle instances.",
    )
    _fake_llm(monkeypatch, model)

    response = asyncio.run(compile_scenario_agent(
        prompt="Increase it to 250 instances",
        current_scenario=current,
        provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
    ))

    assert response.scenario is not None
    assert response.scenario.record_count == 250
    assert response.scenario.checks == current.checks
    assert response.scenario.schemas == current.schemas
    assert model.request_count == 2


def test_initial_custom_schema_replaces_starter_tables(monkeypatch):
    patch = {
        "title": "Three-table claims", "recordCount": 8,
        "workflow": ["Patient", "Claim", "Payment"],
        "schemas": {
            "Patient": {
                "patient_id": {"type": "integer", "constraints": {"primary_key": True}},
                "name": "text", "enrolled_at": "date",
            },
            "Claim": {
                "claim_id": {"type": "integer", "constraints": {"primary_key": True}},
                "patient_id": "foreign_key", "status": "text", "claim_date": "date",
                "__foreign_keys__": {"patient_id": "Patient.patient_id"},
            },
            "Payment": {
                "payment_id": {"type": "integer", "constraints": {"primary_key": True}},
                "claim_id": "foreign_key", "paid_amount": {"type": "float", "constraints": {"min": 1}},
                "payment_date": "date", "__foreign_keys__": {"claim_id": "Claim.claim_id"},
            },
        },
        "paths": [
            {"name": "paid", "steps": ["Patient", "Claim", "Payment"], "weight": 0.5,
             "overrides": {"Claim": {"status": "paid"}}},
            {"name": "denied", "steps": ["Patient", "Claim"], "weight": 0.25,
             "overrides": {"Claim": {"status": "denied"}}},
            {"name": "pending", "steps": ["Patient", "Claim"], "weight": 0.25,
             "overrides": {"Claim": {"status": "pending"}}},
        ],
        "checks": [{"name": "paid_positive", "kind": "semantic_invariant", "table": "Payment",
                    "expression": "paid_amount > 0"}],
    }
    model = _ScenarioToolTestModel(tool_name="create_scenario_draft", tool_args=patch,
                                  final_message="Created three tables and three paths.")
    _fake_llm(monkeypatch, model)
    response = asyncio.run(compile_scenario_agent(
        prompt="Create 8 insurance claims using only Patient, Claim, and Payment",
        provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
    ))
    assert response.scenario is not None
    assert set(response.scenario.schemas) == {"Patient", "Claim", "Payment"}
    assert response.scenario.workflow == ["Patient", "Claim", "Payment"]
    assert len(response.scenario.paths) == 3
    assert model.request_count == 2


def test_agent_does_not_claim_starter_was_created_without_draft_tool(monkeypatch):
    model = TestModel(
        call_tools=[],
        custom_output_args={"message": "The scenario is already configured as requested."},
    )
    _fake_llm(monkeypatch, model)

    response = asyncio.run(compile_scenario_agent(
        prompt="Design 10000 healthcare insurance claims with 15% denied",
        provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
    ))

    assert response.scenario is None
    assert "already configured" not in response.message
    assert "couldn’t create a scenario" in response.message


def test_agent_retries_text_only_confirmation_and_creates_draft(monkeypatch):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    requests = []

    def respond(messages, info):
        requests.append(messages)
        output_name = info.output_tools[0].name
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart(output_name, {"message": "The scenario is already configured."})])
        if len(requests) == 2:
            return ModelResponse(parts=[ToolCallPart("create_scenario_draft", {})])
        return ModelResponse(parts=[ToolCallPart(output_name, {"message": "I created the insurance claims draft."})])

    _fake_llm(monkeypatch, FunctionModel(respond))
    response = asyncio.run(compile_scenario_agent(
        prompt="Design 10000 healthcare insurance claims with 15% denied",
        provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
    ))

    assert len(requests) == 3
    assert response.scenario is not None
    assert response.scenario.record_count == 10000
    assert response.message == "I created the insurance claims draft."


def test_agent_answers_dataset_questions_with_query_dataset(monkeypatch, tmp_path):
    import syda.query

    (tmp_path / "claim.csv").write_text("claim_id,status\n1,approved\n2,approved\n3,denied\n")
    calls = []
    real_query = syda.query.query_dataset

    def spy(output_dir, sql=None, max_rows=200):
        result = real_query(output_dir, sql, max_rows)
        calls.append(result)
        return result

    monkeypatch.setattr(syda.query, "query_dataset", spy)
    current = ScenarioConfiguration.model_validate(DOMAIN_TEMPLATES[0])
    sql = "SELECT status, COUNT(*) AS n FROM claim GROUP BY status ORDER BY status"

    # The second prompt contains "%", which counts as change intent; the answer
    # must still come back instead of "I haven't changed the saved draft yet".
    for prompt in ("How many claims were approved vs denied?", "What % of claims were denied?"):
        calls.clear()
        statuses = []
        _fake_llm(monkeypatch, _ScenarioToolTestModel(
            tool_name="query_dataset", tool_args={"sql": sql},
            final_message="2 claims were approved and 1 was denied.",
        ))
        response = asyncio.run(compile_scenario_agent(
            prompt=prompt,
            current_scenario=current,
            provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
            dataset_dir=str(tmp_path),
            on_status=statuses.append,
        ))
        assert statuses == ["Querying the generated data…"], prompt
        assert calls == [{
            "ok": True, "columns": ["status", "n"],
            "rows": [{"status": "approved", "n": 2}, {"status": "denied", "n": 1}],
            "row_count": 2, "truncated": False,
        }], prompt
        assert response.message == "2 claims were approved and 1 was denied.", prompt


def test_staged_draft_survives_prompt_without_change_keywords(monkeypatch):
    from app.agents.scenario_agent import _has_scenario_change_intent

    prompt = "Double the instance count"
    assert not _has_scenario_change_intent(prompt)  # the keyword gate misses this edit
    current = ScenarioConfiguration.model_validate(DOMAIN_TEMPLATES[0])
    _fake_llm(monkeypatch, _ScenarioToolTestModel(
        tool_name="update_scenario_draft",
        tool_args={"record_count": current.record_count * 2},
        final_message="I doubled the instance count.",
    ))

    response = asyncio.run(compile_scenario_agent(
        prompt=prompt,
        current_scenario=current,
        provider_selection={"agent_model": True, "id": "gemini", "model": "mock", "key": "test"},
    ))

    assert response.scenario is not None
    assert response.scenario.record_count == current.record_count * 2
