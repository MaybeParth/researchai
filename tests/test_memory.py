"""Long-term memory: SQLite store, related-research lookup, suggestions, archived sessions."""

from datetime import date

from fakes import call, say, scripted
from fastapi.testclient import TestClient

from researchai import server
from researchai.config import Settings
from researchai.memory import Store, key_takeaways, keywords
from researchai.search import FakeSearch
from researchai.session import ResearchSession

REPORT = """# EU AI Act and startups

## Executive summary
Summary [S1].

## Key takeaways
- **Deadlines moved.** High-risk duties now apply from 2027 [S1, S2].
- Compliance costs vary widely [S3].

## Costs
Text.
"""


def snapshot(sid, topic, report=REPORT, subs=("What does it cost?",), created="2026-10-20T10:00:00"):
    return {"id": sid, "topic": topic, "created": created, "status": "done", "report": report,
            "plan": {"sub_questions": list(subs)}}


def test_keywords_drop_stopwords_and_plurals():
    assert keywords("The impact of the EU AI Act on startups") == {"eu", "ai", "act", "startup"}


def test_key_takeaways_strips_citations_and_bold():
    assert key_takeaways(REPORT) == ["Deadlines moved. High-risk duties now apply from 2027.",
                                     "Compliance costs vary widely."]
    assert key_takeaways(None) == [] and key_takeaways("# no section") == []


def test_store_round_trip_and_list(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    store.save(snapshot("a", "Topic A"), [{"kind": "plan"}])
    store.save(snapshot("b", "Topic B", created="2026-10-21T10:00:00"), [])
    store.save(snapshot("a", "Topic A v2"), [])  # upsert
    assert [s["topic"] for s in store.list()] == ["Topic B", "Topic A v2"]
    assert store.load("a")["snapshot"]["topic"] == "Topic A v2"
    assert store.load("missing") is None


def test_find_related_matches_keywords_and_skips_unrelated(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    store.save(snapshot("eu", "Impact of the EU AI Act on European startups"), [])
    store.save(snapshot("bat", "Sodium-ion vs lithium-ion batteries"), [])
    store.save(snapshot("norep", "EU AI Act fines", report=None), [])  # unfinished: ignored

    related = store.find_related("EU AI Act enforcement and fines")
    assert [r["id"] for r in related] == ["eu"]
    assert related[0]["takeaways"][0].startswith("Deadlines moved")
    assert store.find_related("Mercury pollution") == []
    assert store.find_related("EU AI Act", exclude_id="eu") == []


async def test_session_uses_memory_and_suggests_topics(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    store.save(snapshot("old", "EU AI Act and startups"), [])
    suggested = []

    async def suggester(topic, report, past):
        suggested.append((topic, past))
        return [{"topic": "AI Act sandboxes", "why": "Open question in the report."}]

    today = date.today().isoformat()
    lead = scripted(
        call("set_research_plan", topic="t", sub_questions=["Fines?"], time_sensitive=False),
        call("task", description="Q1: Fines? (first pass)", subagent_type="researcher"),
        call("finalize_report", markdown="Fines are high [S1]."),
        say("Done."),
    )
    researcher = scripted(
        call("web_search", query="fines", question_number=1),
        call("record_evidence", source_id="S1", question_number=1, summary="s",
             takeaways=["t"], claims=[{"text": "c", "quote": "Fines are high."}]),
        say("Recorded S1."),
    )
    search = FakeSearch(default=[{"url": "https://a.com/1", "title": "A", "score": 0.9,
                                  "content": "Fines are high.", "published_date": today}])
    session = ResearchSession(
        "EU AI Act fines for startups",
        Settings(_env_file=None, anthropic_api_key="x", tavily_api_key="x", max_rounds=1,
                 ask_direction=False),
        search=search, lead_model=lead, researcher_model=researcher,
        store=store, suggester=suggester)

    await session.run()

    memory = [e for e in session.events if e.kind == "memory"]
    assert len(memory) == 1 and "EU AI Act and startups" in memory[0].message
    state = await session.agent.aget_state(session.config)
    first_message = state.values["messages"][0].content
    assert "Previous related research" in first_message and "Deadlines moved" in first_message

    assert suggested == [("EU AI Act fines for startups", ["EU AI Act and startups"])]
    assert session.suggestions[0]["topic"] == "AI Act sandboxes"
    saved = store.load(session.id)
    assert saved["snapshot"]["suggestions"] == session.suggestions
    assert any(e["kind"] == "suggestions" for e in saved["events"])


def test_archived_session_is_readable_and_read_only():
    server.store.save(
        {**snapshot("arch1", "Archived topic"), "paused": False, "pending_question": None},
        [{"kind": "plan", "message": "old plan", "agent": "lead", "data": {},
          "ts": "2026-10-20T10:00:00", "seq": 1}])
    client = TestClient(server.app)

    snap = client.get("/api/sessions/arch1").json()
    assert snap["archived"] is True and snap["topic"] == "Archived topic"
    assert any(s["id"] == "arch1" for s in client.get("/api/sessions").json())
    r = client.post("/api/sessions/arch1/control", json={"action": "pause"})
    assert r.status_code == 400 and "read-only" in r.text


def test_store_delete(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    store.save(snapshot("a", "Topic A"), [])
    assert store.delete("a") is True and store.load("a") is None
    assert store.delete("a") is False


async def test_delete_session_api_stops_and_removes():
    import asyncio

    server.store.save({**snapshot("del1", "To delete"), "paused": False, "pending_question": None}, [])
    live = ResearchSession("Live one", Settings(_env_file=None, anthropic_api_key="x", tavily_api_key="x"),
                           search=FakeSearch(), lead_model=scripted(), researcher_model=scripted())
    server.sessions[live.id] = live
    run = asyncio.create_task(asyncio.sleep(60))  # stands in for a research run
    server._runs[live.id] = run
    client = TestClient(server.app)

    assert client.delete("/api/sessions/del1").json() == {"ok": True}
    assert client.get("/api/sessions/del1").status_code == 404
    assert client.delete(f"/api/sessions/{live.id}").status_code == 200
    await asyncio.sleep(0)
    assert run.cancelled() and live.id not in server.sessions
    assert all(s["id"] not in ("del1", live.id) for s in client.get("/api/sessions").json())
    assert client.delete("/api/sessions/nope").status_code == 404
