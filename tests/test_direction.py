"""The user chooses the research direction before planning (clickable options + own text)."""

import asyncio

from fakes import call, say, scripted

from researchai.config import Settings
from researchai.models import (
    DirectionOption,
    DirectionQuestion,
    describe_answers,
    recommended_answers,
    validate_direction,
)
from researchai.search import FakeSearch
from researchai.session import ResearchSession
from researchai.sources import SourceRegistry
from researchai.tools import make_tools

FOCUS = {"header": "Focus", "question": "Which angles matter most?", "multi_select": True,
         "options": [
             {"label": "Compliance costs", "description": "Money and effort", "recommended": True},
             {"label": "Investor reaction", "description": "VC and funding", "recommended": True},
             {"label": "Legal timeline", "description": "When duties apply"}]}
DEPTH = {"header": "Depth", "question": "How deep?", "options": [
    {"label": "Quick briefing", "description": "One page"},
    {"label": "Deep dive", "description": "Full report", "recommended": True}]}


def q(**kw):
    base = {"header": "Focus", "question": "?", "options": [
        DirectionOption(label="A", description="a", recommended=True),
        DirectionOption(label="B", description="b")]}
    return DirectionQuestion(**{**base, **kw})


# ---------------------------------------------------------------- validation and helpers


def test_validate_direction_rules():
    assert validate_direction([q()]) is None
    assert validate_direction([]) == "Ask 1-3 questions."
    assert validate_direction([q(header=f"H{i}") for i in range(4)]) == "Ask 1-3 questions."
    assert "different header" in validate_direction([q(), q(header="focus")])
    assert "2-4 options" in validate_direction([q(options=q().options[:1])])
    none_rec = [DirectionOption(label=x, description=x) for x in "AB"]
    assert "exactly one recommended" in validate_direction([q(options=none_rec)])
    assert "at least one recommended" in validate_direction([q(options=none_rec, multi_select=True)])
    two_rec = [DirectionOption(label=x, description=x, recommended=True) for x in "AB"]
    assert "exactly one recommended" in validate_direction([q(options=two_rec)])
    assert validate_direction([q(options=two_rec, multi_select=True)]) is None


def test_answer_helpers():
    assert recommended_answers([FOCUS, DEPTH]) == {
        "Focus": ["Compliance costs", "Investor reaction"], "Depth": ["Deep dive"]}
    assert describe_answers({"Focus": ["A", "B"], "Your direction": "EU only"}) == \
        "Focus: A, B · Your direction: EU only"


# ---------------------------------------------------------------- code gates on the tools


async def test_plan_refused_until_direction_is_chosen():
    registry = SourceRegistry()
    tools = make_tools(registry, FakeSearch())
    args = {"topic": "t", "sub_questions": ["q"], "time_sensitive": False}
    assert "ask the user about the research direction first" in \
        await tools.set_research_plan.ainvoke(args)
    registry.direction = {"Focus": ["A"]}
    assert (await tools.set_research_plan.ainvoke(args)).startswith("Plan recorded")


async def test_invalid_questions_rejected_before_asking():
    registry = SourceRegistry()
    tools = make_tools(registry, FakeSearch())
    bad = {**DEPTH, "options": [{**o, "recommended": False} for o in DEPTH["options"]]}
    out = await tools.ask_user.ainvoke({"questions": [bad]})
    assert out.startswith("Rejected:") and registry.direction_key is None


async def test_only_one_round_of_direction_questions():
    registry = SourceRegistry()
    registry.direction_key = "already asked"
    tools = make_tools(registry, FakeSearch())
    assert "already asked" in await tools.ask_user.ainvoke({"questions": [DEPTH]})


# ---------------------------------------------------------------- whole agent


def direction_session(answer_provider):
    lead = scripted(
        call("ask_user", questions=[FOCUS, DEPTH]),
        call("set_research_plan", topic="EU AI Act", sub_questions=["Costs?"],
             time_sensitive=True),
        say("Planned."),
    )
    return ResearchSession(
        "EU AI Act and startups",
        Settings(_env_file=None, anthropic_api_key="x", tavily_api_key="x"),
        answer_provider=answer_provider, search=FakeSearch(),
        lead_model=lead, researcher_model=scripted())


async def tool_reply(session, name):
    state = await session.agent.aget_state(session.config)
    return next(m.content for m in state.values["messages"]
                if getattr(m, "name", None) == name)


async def test_user_choices_reach_the_agent_and_unlock_planning():
    asked = []

    async def choose(payload):
        asked.append(payload)
        await asyncio.sleep(0.05)  # no timeout: a slow answer still counts
        return {"Focus": ["Compliance costs", "Legal timeline"], "Depth": ["Quick briefing"],
                "Your direction": "Focus on German startups"}

    session = direction_session(choose)
    await session.run()

    assert [x["header"] for x in asked[0]["questions"]] == ["Focus", "Depth"]
    reg = session.registry
    assert reg.direction["Your direction"] == "Focus on German startups"
    assert not reg.direction_assumed and reg.assumptions == []
    answer = next(e for e in session.events if e.kind == "answer")
    assert answer.agent == "user" and "Compliance costs, Legal timeline" in answer.message
    reply = await tool_reply(session, "ask_user")
    assert "The user chose this direction" in reply and "German startups" in reply
    assert (await tool_reply(session, "set_research_plan")).startswith("Plan recorded")
    statuses = [e.data.get("status") for e in session.events if e.kind == "session"]
    assert "waiting_for_answer" in statuses
    assert session.snapshot()["direction"] == reg.direction


async def test_without_anyone_to_ask_recommended_options_are_assumed():
    session = direction_session(None)
    await session.run()

    reg = session.registry
    assert reg.direction == {"Focus": ["Compliance costs", "Investor reaction"],
                             "Depth": ["Deep dive"]}
    assert reg.direction_assumed and "recommended options" in reg.assumptions[0]
    assert any(e.kind == "assumption" for e in session.events)


# ---------------------------------------------------------------- report


def test_report_shows_the_chosen_direction():
    registry = SourceRegistry()
    registry.direction = {"Focus": ["Compliance costs"], "Your direction": "German startups"}
    section = registry.direction_section()
    assert section.startswith("## Research direction")
    assert "- **Focus:** Compliance costs" in section and "German startups" in section
    registry.direction_assumed = True
    assert registry.direction_section() == ""  # listed under Assumptions instead
