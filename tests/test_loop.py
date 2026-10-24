"""Whole-agent tests: real Deep Agents graph, scripted models, fake search. No network."""

from datetime import date

from fakes import call, say, scripted

from researchai.config import Settings
from researchai.search import FakeSearch
from researchai.session import ResearchSession

TODAY = date.today().isoformat()


def res(url, content):
    return {"url": url, "title": url, "content": content, "score": 0.9, "published_date": TODAY}


def settings(**kw):
    return Settings(_env_file=None, anthropic_api_key="x", tavily_api_key="x",
                    **{"ask_direction": False, **kw})


async def test_gap_loop_end_to_end():
    lead = scripted(
        call("set_research_plan", topic="Battery costs", sub_questions=["What do they cost?"],
             time_sensitive=True),
        call("task", description="Q1: What do they cost? (first pass)",
             subagent_type="researcher"),
        call("evidence_status"),
        call("finalize_report", markdown="Costs are 50/kWh [S1]."),  # rejected: thin
        call("record_gap", question_number=1, problem="thin",
             why_it_matters="The headline cost figure rests on one vendor's claim.",
             follow_up_queries=["independent battery cost survey"]),
        call("task", description="Q1 follow-up (thin): need an independent source. "
             "Queries: independent battery cost survey", subagent_type="researcher"),
        call("finalize_report", markdown="Costs are 50/kWh [S1], confirmed by a survey [S2]."),
        say("Report delivered."),
    )
    researcher = scripted(
        call("web_search", query="battery cost", question_number=1),
        call("record_evidence", source_id="S1", question_number=1, summary="Vendor says 50.",
             takeaways=["50/kWh"], claims=[{"text": "50/kWh", "quote": "Cells cost 50 per kWh."}]),
        say("Recorded S1."),
        call("web_search", query="independent battery cost survey", question_number=1),
        call("record_evidence", source_id="S2", question_number=1, summary="Survey agrees.",
             takeaways=["~50/kWh"], claims=[{"text": "about 50", "quote": "Average 52 per kWh."}]),
        say("Recorded S2."),
    )
    search = FakeSearch({
        "survey": [res("https://survey.org/report", "Average 52 per kWh.")],
        "battery cost": [res("https://vendor.com/blog", "Cells cost 50 per kWh.")],
    })
    session = ResearchSession("Battery costs", settings(), search=search,
                              lead_model=lead, researcher_model=researcher)

    report = await session.run()

    kinds = [e.kind for e in session.events]
    assert kinds.index("report_rejected") < kinds.index("gap") < kinds.index("round") \
        < kinds.index("report_accepted")
    assert session.registry.round == 2
    assert "## How this report was strengthened" in report
    assert "rests on one vendor's claim" in report
    assert "1 source from 1 site" in report and "2 sources from 2 sites" in report
    assert "[S2]" in report and "https://survey.org/report" in report
