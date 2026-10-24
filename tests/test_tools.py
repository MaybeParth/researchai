from conftest import TODAY, result

from researchai.search import FakeSearch
from researchai.sources import SourceRegistry
from researchai.tools import make_tools


def setup(responses=None, default=None):
    # max_rounds=1: these tests cover citations; the gap gate is tested in test_gaps.py
    reg = SourceRegistry(today=TODAY, sources_per_query=3, max_rounds=1)
    search = FakeSearch(responses, default)
    return reg, search, make_tools(reg, search, require_direction=False)


async def plan(tools, time_sensitive=True):
    await tools.set_research_plan.ainvoke({
        "topic": "EU AI Act", "sub_questions": ["Obligations?", "Costs?"],
        "time_sensitive": time_sensitive,
    })


async def test_search_requires_plan_and_valid_question():
    _, search, tools = setup(default=[result("https://a.com/1")])
    out = await tools.web_search.ainvoke({"query": "x", "question_number": 1})
    assert "No research plan" in out and search.calls == []
    await plan(tools)
    out = await tools.web_search.ainvoke({"query": "x", "question_number": 5})
    assert "not in the plan" in out


async def test_search_returns_only_kept_sources_with_ids():
    reg, _, tools = setup(default=[
        result("https://a.com/1", title="Fresh", published="2026-10-01"),
        result("https://b.com/2", title="Stale", published="2020-01-01"),
    ])
    await plan(tools)
    out = await tools.web_search.ainvoke({"query": "fines", "question_number": 1})
    assert "[S1] Fresh (a.com, 2026-10-01)" in out
    assert "Stale" not in out
    assert len(reg.dropped) == 1


async def test_search_failure_becomes_feedback():
    class Broken:
        async def search(self, *a, **k):
            raise RuntimeError("timeout")

    reg = SourceRegistry(today=TODAY)
    tools = make_tools(reg, Broken(), require_direction=False)
    await plan(tools)
    out = await tools.web_search.ainvoke({"query": "x", "question_number": 1})
    assert out.startswith("Search failed")


async def test_full_tool_flow_produces_cited_report():
    reg, _, tools = setup({
        "obligations": [result("https://a.com/1", content="Startups must register systems.")],
        "costs": [result("https://b.com/2", content="Costs reach 50k euros.")],
    })
    await plan(tools)
    await tools.web_search.ainvoke({"query": "obligations", "question_number": 1})
    await tools.web_search.ainvoke({"query": "costs", "question_number": 2})
    for sid, q, quote in [("S1", 1, "Startups must register systems."),
                          ("S2", 2, "Costs reach 50k euros.")]:
        out = await tools.record_evidence.ainvoke({
            "source_id": sid, "question_number": q, "summary": "s", "takeaways": ["t"],
            "claims": [{"text": "c", "quote": quote}],
        })
        assert out.startswith("Recorded") and "not found verbatim" not in out

    rejected = await tools.finalize_report.ainvoke({"markdown": "Only [S1] and [S7]."})
    assert rejected.startswith("REJECTED") and "[S7]" in rejected and "Q2" in rejected

    accepted = await tools.finalize_report.ainvoke({"markdown": "A [S1]. B [S2]."})
    assert accepted.startswith("ACCEPTED")
    assert reg.report.endswith("\n") and "## Sources" in reg.report
