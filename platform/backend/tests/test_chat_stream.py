"""Exercise stream timing and background persistence independently of model latency."""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database
from app.api import agent
from app.agents.scenario_agent import DOMAIN_TEMPLATES
from app.models.entities import ChatConversation
from app.models.scenario import ChatResponse, ScenarioConfiguration
from app.models.user import User


class RequestConnection:
    disconnected = False

    async def is_disconnected(self):
        return self.disconnected


@pytest.fixture
def conversation_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat.db'}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    user = User(id=uuid.uuid4(), email="stream@example.com", hashed_password="test-only",
                is_active=True, is_superuser=False, is_verified=True, display_name="Stream Test")
    conversation_id = str(uuid.uuid4())
    with factory() as db:
        db.add(user)
        db.add(ChatConversation(id=conversation_id, user_id=user.id, title="Streaming test", messages=[], runs=[]))
        db.commit()
    monkeypatch.setattr(agent, "SessionLocal", factory)
    monkeypatch.setattr(agent, "resolve_provider", lambda *_: {"id": "test"})
    monkeypatch.setattr(agent, "CHAT_HEARTBEAT_SECONDS", 0.01)
    yield factory, user, conversation_id
    engine.dispose()


def request_for(conversation_id):
    return agent.ChatStreamRequest(
        conversationId=conversation_id,
        messages=[{"role": "user", "parts": [{"type": "text", "text": "Create 8 claims"}]}],
    )


def decode_events(chunks):
    return [json.loads(chunk.removeprefix("data: "))
            for chunk in chunks if chunk.startswith("data: ")]


def test_heartbeats_arrive_before_compilation_and_preserve_ui_protocol(monkeypatch, conversation_db):
    factory, user, conversation_id = conversation_db

    async def exercise():
        release = asyncio.Event()

        async def compile_later(**_):
            await release.wait()
            return ChatResponse(message="Draft ready", scenario=ScenarioConfiguration.model_validate(DOMAIN_TEMPLATES[0]))

        monkeypatch.setattr(agent, "compile_scenario_agent", compile_later)
        with factory() as db:
            response = await agent.chat_with_agent(request_for(conversation_id), RequestConnection(), db, user)
            stream = response.body_iterator
            # Immediate and periodic bytes must arrive while the provider is blocked.
            first = await asyncio.wait_for(anext(stream), 0.5)
            second = await asyncio.wait_for(anext(stream), 0.5)
            assert first == second == ": keep-alive\n\n"
            assert not release.is_set()
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
            release.set()
            chunks = [chunk async for chunk in stream]
        events = decode_events(chunks)
        assert events[0]["type"] == "start"
        assert "".join(e["delta"] for e in events if e["type"] == "text-delta") == "Draft ready"
        assert next(e for e in events if e["type"] == "data-scenario")["data"]["schemas"]
        assert events[-1] == {"type": "finish", "finishReason": "stop"}

    asyncio.run(exercise())
    with factory() as db:
        conversation = db.get(ChatConversation, conversation_id)
        assert conversation.messages[-1]["role"] == "assistant"
        assert conversation.scenario_draft is not None


def test_disconnected_stream_still_persists_completed_draft(monkeypatch, conversation_db):
    factory, user, conversation_id = conversation_db

    async def exercise():
        release = asyncio.Event()

        async def compile_later(**_):
            await release.wait()
            return ChatResponse(message="Saved after disconnect")

        monkeypatch.setattr(agent, "compile_scenario_agent", compile_later)
        connection = RequestConnection()
        with factory() as db:
            response = await agent.chat_with_agent(request_for(conversation_id), connection, db, user)
            assert await anext(response.body_iterator) == ": keep-alive\n\n"
            tasks = list(agent._compilation_tasks)
            assert len(tasks) == 1
            connection.disconnected = True
            with pytest.raises(StopAsyncIteration):
                await anext(response.body_iterator)
            assert not tasks[0].cancelled()
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 0.5)
            assert not agent._compilation_tasks

    asyncio.run(exercise())
    with factory() as db:
        messages = db.get(ChatConversation, conversation_id).messages
        assert messages[-1]["parts"][0]["text"] == "Saved after disconnect"


def test_provider_failure_remains_a_typed_stream_error(monkeypatch, conversation_db):
    factory, user, conversation_id = conversation_db

    async def fail(**_):
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr(agent, "compile_scenario_agent", fail)

    async def exercise():
        with factory() as db:
            response = await agent.chat_with_agent(request_for(conversation_id), RequestConnection(), db, user)
            events = decode_events([chunk async for chunk in response.body_iterator])
            assert events == [{"type": "error", "errorText": "Provider unavailable"}]

    asyncio.run(exercise())


def test_cancelling_stream_wait_does_not_cancel_compilation(monkeypatch, conversation_db):
    factory, user, conversation_id = conversation_db

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()

        async def compile_later(**_):
            started.set()
            await release.wait()
            return ChatResponse(message="Saved despite stream cancellation")

        monkeypatch.setattr(agent, "compile_scenario_agent", compile_later)
        with factory() as db:
            response = await agent.chat_with_agent(request_for(conversation_id), RequestConnection(), db, user)
            assert await anext(response.body_iterator) == ": keep-alive\n\n"
            await started.wait()
            tasks = list(agent._compilation_tasks)
            consuming = asyncio.create_task(anext(response.body_iterator))
            await asyncio.sleep(0)
            consuming.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consuming
            assert not tasks[0].cancelled()
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 0.5)

    asyncio.run(exercise())
    with factory() as db:
        assert db.get(ChatConversation, conversation_id).messages[-1]["parts"][0]["text"] == "Saved despite stream cancellation"


def test_latest_dataset_dir_picks_newest_completed_run_owned_by_user(monkeypatch, conversation_db, tmp_path):
    from app.models.entities import JobRecord

    factory, user, conversation_id = conversation_db
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    other_user = uuid.uuid4()
    jobs = {"old": (user.id, "complete"), "new": (user.id, "complete"),
            "running": (user.id, "generating"), "foreign": (other_user, "complete")}
    with factory() as db:
        for job_id, (owner, status) in jobs.items():
            db.add(JobRecord(id=job_id, user_id=owner, status=status, scenario={}))
            (tmp_path / job_id).mkdir()
            (tmp_path / job_id / "claim.csv").write_text("id\n1\n")
        db.commit()
        conversation = db.get(ChatConversation, conversation_id)

        conversation.runs = [{"jobId": "old"}, {"jobId": "new"}, {"jobId": "running"}, {"jobId": "foreign"}]
        assert agent._latest_dataset_dir(db, conversation, user.id) == str(tmp_path / "new")

        conversation.runs = [{"jobId": "foreign"}, {"jobId": "running"}]
        assert agent._latest_dataset_dir(db, conversation, user.id) is None


def test_tool_status_streams_live_as_transient_parts(monkeypatch, conversation_db):
    import threading

    factory, user, conversation_id = conversation_db

    async def exercise():
        release = asyncio.Event()

        async def compile_with_tools(on_status=None, **_):
            # Sync tools run in a worker thread; report from one like pydantic-ai does.
            thread = threading.Thread(target=on_status, args=("Drafting the scenario…",))
            thread.start()
            thread.join()
            on_status("Fixing validation issues…")
            await release.wait()
            return ChatResponse(message="Draft ready, with two tables.")

        monkeypatch.setattr(agent, "compile_scenario_agent", compile_with_tools)
        with factory() as db:
            response = await agent.chat_with_agent(request_for(conversation_id), RequestConnection(), db, user)
            stream = response.body_iterator
            assert await asyncio.wait_for(anext(stream), 0.5) == ": keep-alive\n\n"
            # Both statuses arrive while the agent is still working.
            live = [await asyncio.wait_for(anext(stream), 0.5) for _ in range(2)]
            assert decode_events(live) == [
                {"type": "data-status", "data": {"message": "Drafting the scenario…"}, "transient": True},
                {"type": "data-status", "data": {"message": "Fixing validation issues…"}, "transient": True},
            ]
            release.set()
            events = decode_events([chunk async for chunk in stream])
        # The final text arrives whole, not word-by-word with artificial delays.
        assert [e["delta"] for e in events if e["type"] == "text-delta"] == ["Draft ready, with two tables."]

    asyncio.run(exercise())
    with factory() as db:
        parts = db.get(ChatConversation, conversation_id).messages[-1]["parts"]
        assert all(p["type"] != "data-status" for p in parts)
