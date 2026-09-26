import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from app.agents.scenario_agent import (
    DOMAIN_TEMPLATES,
    ScenarioAgentReply,
    compile_scenario_agent,
)
from app.models.scenario import ScenarioConfiguration


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
