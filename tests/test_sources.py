import threading

from conftest import TODAY, result

from researchai.models import Claim, Evidence, ResearchPlan
from researchai.sources import canonical_url, cited_ids, parse_date, quote_in_text, triage


def evidence(sid, q=1, quote="content"):
    return Evidence(source_id=sid, question_number=q, summary="s", takeaways=["t"],
                    claims=[Claim(text="c", quote=quote)])


# ---------------------------------------------------------------- triage


def test_canonical_url_strips_tracking_and_www():
    assert canonical_url("http://www.Example.com/a/?utm_source=x&id=2#top") == \
        canonical_url("https://example.com/a?id=2")


def test_parse_date_formats():
    assert parse_date("Tue, 10 Mar 2026 15:52:40 GMT").isoformat() == "2026-03-10"
    assert parse_date("2026-03-10").isoformat() == "2026-03-10"
    assert parse_date(None) is None and parse_date("garbage") is None


def test_triage_drops_duplicates_within_batch(plan):
    out = triage([result("https://a.com/x"), result("https://a.com/x?utm_medium=y")],
                 plan, TODAY, limit=5)
    assert [s.kept for s in out] == [True, False]
    assert "duplicate" in out[1].reasons[0]


def test_triage_drops_stale_only_when_time_sensitive(plan):
    old = [result("https://a.com/old", published="2022-01-01")]
    out = triage(old, plan, TODAY, limit=5)
    assert not out[0].kept and "older than the 365-day window" in out[0].reasons[0]

    timeless = plan.model_copy(update={"time_sensitive": False})
    assert triage(old, timeless, TODAY, limit=5)[0].kept


def test_triage_ranks_undated_below_dated_recent(plan):
    out = triage([result("https://a.com/undated"),
                  result("https://b.com/recent", published="2026-10-01")], plan, TODAY, limit=1)
    kept = [s for s in out if s.kept]
    assert [s.domain for s in kept] == ["b.com"]
    undated = next(s for s in out if s.domain == "a.com")
    assert "publish date unknown" in undated.reasons
    assert "ranked below the kept results" in undated.reasons


def test_triage_drops_low_relevance_and_boosts_official(plan):
    out = triage([result("https://spam.com/a", score=0.1),
                  result("https://digital-strategy.ec.europa.eu/x", score=0.5),
                  result("https://blog.com/y", score=0.55)], plan, TODAY, limit=1)
    assert out[0].domain.endswith("europa.eu") and out[0].kept
    assert any("low relevance" in r for s in out for r in s.reasons)


# ---------------------------------------------------------------- registry


def test_registry_assigns_ids_and_reuses_known_sources(registry):
    first = registry.add_results([result("https://a.com/1"), result("https://b.com/2")], 1, "q")
    assert [s.id for s in first] == ["S1", "S2"]
    again = registry.add_results([result("https://www.a.com/1/")], 2, "q2")
    assert [s.id for s in again] == ["S1"]
    assert registry.sources["S1"].found_for == [1, 2]


def test_dropped_sources_summarised_in_one_event(registry, events):
    registry.add_results([result("https://a.com/1"), result("https://a.com/1"),
                          result("https://b.com/old", published="2020-01-01"),
                          result("https://c.com/x", score=0.1)], 1, "q")
    dropped = [e for e in events if e.kind == "dropped"]
    assert len(dropped) == 1
    assert "3 dropped" in dropped[0].message
    assert {"duplicate", "outdated", "off-topic"} <= set(dropped[0].message.replace(",", "")
                                                          .split())
    assert len(dropped[0].data["sources"]) == 3


def test_registry_ids_unique_under_parallel_calls(registry):
    def worker(i):
        registry.add_results([result(f"https://site{i}.com/{j}") for j in range(3)], 1, "q")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(registry.sources) == 30
    assert set(registry.sources) == {f"S{i}" for i in range(1, 31)}


def test_evidence_rejects_unknown_excluded_and_bad_question(registry):
    registry.add_results([result("https://a.com/1")], 1, "q")
    assert "Unknown source" in registry.add_evidence(evidence("S99"))
    assert "not in the research plan" in registry.add_evidence(evidence("S1", q=7))
    registry.sources["S1"].excluded = True
    assert "excluded" in registry.add_evidence(evidence("S1"))
    assert registry.evidence == []


def test_evidence_marks_quotes_verified(registry):
    registry.add_results([result("https://a.com/1", content="Fines reach 7% of turnover.")],
                         1, "q")
    registry.add_evidence(evidence("S1", quote="fines reach “7% of turnover”"))
    registry.add_evidence(evidence("S1", quote="made up"))
    assert [e.claims[0].verified for e in registry.evidence] == [True, False]


def test_quote_in_text_normalises_whitespace_and_case():
    assert quote_in_text("Hello   World", "say hello world now")
    assert not quote_in_text("", "anything")


# ---------------------------------------------------------------- citations / report gate


def test_cited_ids_handles_groups_and_order():
    assert cited_ids("A [S2]. B [S1, S3]. C [S2]") == ["S2", "S1", "S3"]


def _with_evidence(registry):
    registry.add_results([result("https://a.com/1"), result("https://b.com/2")], 1, "q")
    registry.add_results([result("https://c.com/3")], 2, "q")
    registry.add_evidence(evidence("S1", q=1))
    registry.add_evidence(evidence("S3", q=2))


def test_report_gate_requires_plan_and_evidence(events):
    from researchai.sources import SourceRegistry

    reg = SourceRegistry(today=TODAY)
    assert "No research plan" in reg.check_report("x [S1]")[0]
    reg.set_plan(ResearchPlan(topic="t", sub_questions=["q"], time_sensitive=False))
    assert "No evidence" in reg.check_report("x [S1]")[0]


def test_report_gate_rejects_bad_citations(registry):
    _with_evidence(registry)
    problems = registry.check_report("Claim [S1]. Other [S9].")
    assert any("[S9] does not exist" in p for p in problems)
    assert any(p.startswith("Q2") and "none of it is cited" in p for p in problems)


def test_report_gate_rejects_excluded_and_uncited(registry):
    _with_evidence(registry)
    registry.sources["S3"].excluded = True
    assert any("excluded" in p for p in registry.check_report("A [S1]. B [S3]."))
    assert registry.check_report("No citations at all.")[0].startswith("The report has no")


def test_finalize_accepts_valid_report_and_appends_sources(registry, events):
    registry.max_rounds = 1  # isolate citation checks from the gap gate
    _with_evidence(registry)
    assert registry.finalize("# R\nA [S1]. B [S3].") == []
    assert "## Sources" in registry.report
    assert "[S1]" in registry.report and "https://c.com/3" in registry.report
    assert "https://b.com/2" not in registry.report  # uncited sources are not listed
    assert events[-1].kind == "report_accepted"


def test_finalize_rejection_keeps_no_report(registry, events):
    _with_evidence(registry)
    problems = registry.finalize("A [S1].")
    assert problems and registry.report is None
    assert events[-1].kind == "report_rejected"
