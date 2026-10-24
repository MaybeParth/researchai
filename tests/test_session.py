"""Stream-to-event translation, using update shapes observed in the live smoke test."""

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from researchai.config import Settings
from researchai.search import FakeSearch
from researchai.session import ResearchSession


def make_session():
    fake = GenericFakeChatModel(messages=iter([]))
    return ResearchSession("topic", Settings(_env_file=None), search=FakeSearch(),
                           lead_model=fake, researcher_model=fake)


def test_updates_become_events():
    s = make_session()
    s._handle_update("lead", {"PatchToolCallsMiddleware.before_agent": None})
    s._handle_update("lead", {"tools": {
        "todos": [{"content": "Q1: Costs?", "status": "in_progress"}],
        "messages": [ToolMessage(content="ok", tool_call_id="1")],
    }})
    s._handle_update("lead", {"model": {"messages": [AIMessage(content="", tool_calls=[
        {"name": "task", "id": "2",
         "args": {"description": "Q1: Costs? Focus on SMEs", "subagent_type": "researcher"}},
        {"name": "web_search", "id": "3", "args": {"query": "x", "question_number": 1}},
    ])]}})
    s._handle_update("lead", {"model": {"messages": [AIMessage(content="Done.")]}})
    s._handle_update("researcher", {"model": {"messages": [AIMessage(content="sub reply")]}})

    assert [e.kind for e in s.events] == ["todos", "delegate", "agent"]
    assert "Q1: Costs? [in_progress]" in s.events[0].message
    assert "researcher: Q1: Costs?" in s.events[1].message
    assert s.events[2].message == "Done."


def test_registry_events_flow_into_session_log():
    s = make_session()
    s.registry.emit("search", "hello", agent="researcher")
    assert s.events[-1].kind == "search"
