"""User controls: pause/resume, skip, exclude (+ report revision), answers, and the API."""

import asyncio
from datetime import date

from conftest import result
from fakes import call, say, scripted
from fastapi.testclient import TestClient

from researchai import server
from researchai.config import Settings
from researchai.controls import Controls
from researchai.models import Claim, Evidence
from researchai.search import FakeSearch
from researchai.session import ResearchSession
from researchai.tools import make_tools

TODAY = date.today().isoformat()


def settings(**kw):
    return Settings(_env_file=None, anthropic_api_key="x", tavily_api_key="x",
                    **{"ask_direction": False, **kw})


def add(registry, url, q):
    [src] = registry.add_results([result(url, content="Fact.", published="2026-10-01")], q, "q")
    registry.add_evidence(Evidence(source_id=src.id, question_number=q, summary="s",
                                   takeaways=["t"], claims=[Claim(text="c", quote="Fact.")]))
    return src.id


# ---------------------------------------------------------------- pause / resume


async def test_pause_blocks_tools_until_resume(registry):
    controls = Controls()
    search = FakeSearch(default=[result("https://a.com/1")])
    tools = make_tools(registry, search, controls)

    controls.pause()
    call_task = asyncio.create_task(
        tools.web_search.ainvoke({"query": "x", "question_number": 1}))
    await asyncio.sleep(0.05)
    assert not call_task.done() and search.calls == []

    controls.resume()
    out = await asyncio.wait_for(call_task, 1)
    assert "[S1]" in out and search.calls == ["x"]


# ---------------------------------------------------------------- skip


async def test_skipped_question_short_circuits_tools(registry, events):
    tools = make_tools(registry, FakeSearch(default=[result("https://a.com/1")]))
    assert registry.skip_question(2) is None
    assert events[-1].kind == "control" and events[-1].agent == "user"

    out = await tools.web_search.ainvoke({"query": "x", "question_number": 2})
    assert "skipped by the user" in out
    sid = add(registry, "https://b.com/1", 1)
    rejected = registry.add_evidence(Evidence(source_id=sid, question_number=2, summary="s",
                                              takeaways=[], claims=[]))
    assert "skipped" in rejected
    assert "skipped" in registry.record_gap(2, "missing", "Long enough reason here.", ["q"])


def test_skipped_question_is_not_flagged_or_required(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 1)
    registry.skip_question(2)  # Q2 has no evidence: would otherwise be "missing"
    q1, q2 = registry.question_statuses()
    assert q1.flags == [] and q2.skipped and q2.flags == []
    assert registry.finalize("A [S1].") == []
    assert "Q2** (Costs?): skipped by the user" in registry.report


def test_skipping_drops_pending_gap_from_gate(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 1)
    registry.record_gap(2, "missing", "Without it the cost section is empty.", ["q"])
    assert registry.pending_gaps()
    registry.skip_question(2)
    assert registry.pending_gaps() == []


# ---------------------------------------------------------------- exclude


def test_excluded_source_leaves_status_and_citations(registry):
    sid = add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 1)
    assert registry.exclude_source("S99") == "Unknown source S99."
    registry.exclude_source(sid)
    assert registry.question_status(1).sources == 1
    assert any("excluded by the user" in p for p in registry.check_report("A [S1]. B [S2]."))


async def test_exclude_after_report_triggers_revision():
    lead = scripted(
        call("set_research_plan", topic="t", sub_questions=["What?"], time_sensitive=False),
        call("task", description="Q1: What? (first pass)", subagent_type="researcher"),
        call("finalize_report", markdown="Claim one [S1]. Claim two [S2]."),
        say("Done."),
        # revision turn, after the user excludes S1
        call("finalize_report", markdown="Claim two [S2]."),
        say("Revised."),
    )
    researcher = scripted(
        call("web_search", query="what", question_number=1),
        call("record_evidence", source_id="S1", question_number=1, summary="s",
             takeaways=["t"], claims=[{"text": "c", "quote": "Fact one."}]),
        call("record_evidence", source_id="S2", question_number=1, summary="s",
             takeaways=["t"], claims=[{"text": "c", "quote": "Fact two."}]),
        say("Recorded S1, S2."),
    )
    search = FakeSearch(default=[
        {"url": "https://a.com/1", "title": "A", "content": "Fact one.", "score": 0.9,
         "published_date": TODAY},
        {"url": "https://b.com/2", "title": "B", "content": "Fact two.", "score": 0.8,
         "published_date": TODAY},
    ])
    session = ResearchSession("t", settings(max_rounds=1), search=search,
                              lead_model=lead, researcher_model=researcher)
    await session.run()
    assert session.registry.report_version == 1 and "https://a.com/1" in session.registry.report

    assert session.exclude("S1") is None
    assert "S1" in session.revision_needed()
    await session.revise_if_needed()

    report = session.registry.report
    assert session.registry.report_version == 2 and session.status == "done"
    assert "[S1]" not in report and "https://a.com/1" not in report
    assert session.revision_needed() is None


# ---------------------------------------------------------------- answers and events


async def test_web_answer_flow():
    session = ResearchSession("t", settings(), search=FakeSearch(),
                              lead_model=scripted(), researcher_model=scripted())
    assert session.submit_answer({"Focus": ["x"]}) == "No question is waiting for an answer"
    session.pending_question = {"questions": [{"header": "Focus", "question": "q", "options": []}]}
    waiting = asyncio.create_task(session.await_submitted_answer({}))
    await asyncio.sleep(0)
    assert session.submit_answer({"Depth": ["x"]}) == "Unknown question: Depth"
    assert session.submit_answer({"Focus": [" "]}).startswith("Choose at least one")
    assert session.submit_answer({"Focus": ["Costs"], "Your direction": " EU only "}) is None
    assert await waiting == {"Focus": ["Costs"], "Your direction": "EU only"}


def test_subscribers_get_backlog_then_live_events():
    session = ResearchSession("t", settings(), search=FakeSearch(),
                              lead_model=scripted(), researcher_model=scripted())
    session.registry.emit("search", "first")
    backlog, queue = session.subscribe()
    session.registry.emit("search", "second")
    assert [e.message for e in backlog] == ["first"]
    assert queue.get_nowait().message == "second"
    assert [e.seq for e in session.events] == [1, 2]


# ---------------------------------------------------------------- API


def test_control_api():
    session = ResearchSession("t", settings(), search=FakeSearch(),
                              lead_model=scripted(), researcher_model=scripted())
    server.sessions[session.id] = session
    client = TestClient(server.app)
    url = f"/api/sessions/{session.id}/control"
    try:
        assert client.post("/api/sessions/nope/control", json={"action": "pause"}).status_code \
            == 404
        assert client.post(url, json={"action": "pause"}).json() == {"ok": True}
        assert session.controls.paused
        assert client.get(f"/api/sessions/{session.id}").json()["paused"] is True
        client.post(url, json={"action": "resume"})
        assert not session.controls.paused

        bad_skip = client.post(url, json={"action": "skip", "question_number": 1})
        assert bad_skip.status_code == 400 and "not in the research plan" in bad_skip.text
        assert client.post(url, json={"action": "answer", "answers": {"Focus": ["x"]}}).status_code == 400
        assert client.get("/api/sessions").json()[0]["id"] == session.id
    finally:
        server.sessions.pop(session.id)
