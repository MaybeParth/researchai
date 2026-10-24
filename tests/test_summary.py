"""The research summary: built from a snapshot + events, rendered to Markdown, served by the API."""

from fastapi.testclient import TestClient

from researchai import server
from researchai.summary import build_summary, citation_counts, report_section, summary_markdown

REPORT = """# EU AI Act and startups

## Executive summary
Costs vary widely [S1, S2].

## Key takeaways
- Deadlines moved to 2027 [S1].
- Costs are disputed [S2].

## Where sources disagree
- €29k vs €300k [S1, S2].

## Open questions
- No startup survey.

## Sources

- **[S1]** [A](https://a.com) — a.com
"""

STATUS = {"sources": 3, "sites": 2, "newest": "2026-10-01", "quotes_verified": 5,
          "quotes_total": 6}

SNAPSHOT = {
    "id": "s1", "topic": "EU AI Act and startups", "status": "done",
    "created": "2026-10-22T10:00:00", "round": 2, "max_rounds": 2, "report_version": 2,
    "plan": {"time_sensitive": True},
    "questions": [
        {"number": 1, "question": "Costs?", **STATUS, "flags": [], "skipped": False},
        {"number": 2, "question": "Support?", "sources": 0, "sites": 0, "newest": None,
         "quotes_verified": 0, "quotes_total": 0, "flags": [], "skipped": True},
    ],
    "todos": [{"content": "Q1: Costs?", "status": "completed"}],
    "gaps": [{"question_number": 1, "problem": "thin", "why_it_matters": "One vendor only.",
              "follow_up_queries": ["independent study"], "round": 2,
              "before": {**STATUS, "sources": 1, "sites": 1}}],
    "sources": [
        {"id": "S1", "title": "A", "url": "https://a.com", "domain": "a.com", "evidence": 2,
         "excluded": False},
        {"id": "S2", "title": "B", "url": "https://b.com", "domain": "b.com", "evidence": 1,
         "excluded": False},
        {"id": "S3", "title": "C", "url": "https://c.com", "domain": "c.com", "evidence": 0,
         "excluded": True},
    ],
    "dropped": 3, "direction": {"Focus": ["Costs"], "Your direction": "Germany"},
    "direction_assumed": False, "assumptions": [], "report": REPORT,
    "related": [{"id": "old", "topic": "Earlier", "created": "2026-10-20T09:00:00"}],
    "suggestions": [{"topic": "Sandboxes", "why": "Open question."}],
}


def ev(kind, message, ts, agent="lead", **data):
    return {"kind": kind, "message": message, "agent": agent, "data": data, "ts": ts}


EVENTS = [
    ev("session", "Research started", "2026-10-22T10:00:00", "system", status="running"),
    ev("answer", "You chose: Focus: Costs", "2026-10-22T10:00:20", "user"),
    ev("dropped", "3 dropped", "2026-10-22T10:00:40", "researcher", sources=[
        {"reasons": ["older than the 365-day window"]}, {"reasons": ["duplicate result"]},
        {"reasons": ["publish date unknown", "ranked below the kept results"]}]),
    ev("report_rejected", "rejected", "2026-10-22T10:01:30", problems=["Q1 evidence is thin."]),
    ev("control", "User skipped Q2", "2026-10-22T10:01:40", "user"),
    ev("control", "User excluded S3 (c.com)", "2026-10-22T10:02:10", "user"),
    ev("session", "Revising the report", "2026-10-22T10:02:11", "system", status="revising"),
    ev("session", "Research finished", "2026-10-22T10:02:45", "system", status="done"),
]


def test_report_helpers():
    assert report_section(REPORT, "executive summary") == "Costs vary widely [S1, S2]."
    assert report_section(REPORT, "Missing") == "" and report_section(None, "x") == ""
    assert citation_counts(REPORT) == {"S1": 3, "S2": 3}  # the Sources list isn't counted


def test_build_summary_covers_the_whole_session():
    s = build_summary(SNAPSHOT, EVENTS)

    o = s["overview"]
    assert (o["duration_s"], o["rounds_used"], o["report_version"], o["sources_used"]) == \
        (165, 2, 2, 2)
    assert s["direction"]["answers"]["Your direction"] == "Germany"
    assert s["findings"]["takeaways"] == ["Deadlines moved to 2027 [S1].", "Costs are disputed [S2]."]
    assert "€29k vs €300k" in s["findings"]["disagreements"]

    q1, q2 = s["questions"]
    assert q1["status"] == "completed" and q1["measure"].startswith("3 sources from 2 sites")
    assert q2["status"] == "skipped"

    [gap] = s["adaptation"]["gaps"]
    assert gap["before"].startswith("1 source from 1 site") and gap["after"].startswith("3 sources")
    assert not gap["still_open"]
    assert s["adaptation"]["rejected_drafts"][0]["problems"] == ["Q1 evidence is thin."]

    src = s["sources"]
    assert (src["used"], src["candidates"], src["dropped"]) == (2, 1, 3)
    assert src["dropped_by_reason"] == {"outdated": 1, "duplicate": 1, "lower-ranked": 1}
    assert src["excluded"][0]["id"] == "S3" and src["top_cited"][0]["count"] == 3

    assert [a["text"] for a in s["actions"]] == [
        "You chose: Focus: Costs", "User skipped Q2", "User excluded S3 (c.com)",
        "Revising the report"]
    assert s["context"]["suggestions"][0]["topic"] == "Sandboxes"


def test_build_summary_tolerates_a_session_in_progress():
    s = build_summary({"id": "x", "topic": "t", "status": "running", "created": None}, [])
    assert s["questions"] == [] and s["findings"]["takeaways"] == []
    assert s["overview"]["duration_s"] == 0
    old_gap = {**SNAPSHOT, "gaps": [{**SNAPSHOT["gaps"][0], "before": None}]}
    assert build_summary(old_gap, EVENTS)["adaptation"]["gaps"][0]["before"] == "—"


def test_summary_markdown_has_every_section():
    md = summary_markdown(build_summary(SNAPSHOT, EVENTS))
    for heading in ["# Research summary: EU AI Act and startups", "## Your direction",
                    "## Findings", "## Plan and evidence", "## How the agent adapted",
                    "## Sources", "## Your actions", "## Built on earlier research",
                    "## What to research next"]:
        assert heading in md
    assert "took 2 min 45 s" in md and "revised (v2)" in md
    assert "sent back 1 draft" in md and "[S1]" in md
    assert "3 dropped (1 outdated, 1 duplicate, 1 lower-ranked)" in md


def test_summary_api_for_saved_sessions():
    server.store.save({**SNAPSHOT, "id": "sum1"}, EVENTS)
    client = TestClient(server.app)
    assert client.get("/api/sessions/sum1/summary").json()["overview"]["topic"] == SNAPSHOT["topic"]
    md = client.get("/api/sessions/sum1/summary.md")
    assert md.status_code == 200 and md.text.startswith("# Research summary")
    assert 'filename="eu-ai-act-and-startups-summary.md"' in md.headers["content-disposition"]
    assert client.get("/api/sessions/nope/summary").status_code == 404


def test_duration_excludes_paused_time():
    events = [
        ev("session", "Research started", "2026-10-22T10:00:00", "system"),
        ev("control", "User paused", "2026-10-22T10:00:30", "user", action="pause"),
        ev("control", "User resumed", "2026-10-22T10:05:30", "user", action="resume"),
        ev("session", "Research finished", "2026-10-22T10:06:00", "system"),
    ]
    assert build_summary(SNAPSHOT, events)["overview"]["duration_s"] == 60  # 6 min minus 5 paused
