"""Evidence review, gap recording, round limits and the gap gate (registry level)."""

from conftest import result

from researchai.models import Claim, Evidence


def add(registry, url, q, published="2026-10-01", content="Fact one.", quote="Fact one."):
    [src] = [s for s in registry.add_results([result(url, content=content, published=published)],
                                             q, "query") if s.url == url]
    registry.add_evidence(Evidence(source_id=src.id, question_number=q, summary="s",
                                   takeaways=["t"], claims=[Claim(text="c", quote=quote)]))
    return src.id


WHY = "The cost conclusion would rest on a single vendor blog."


# ---------------------------------------------------------------- measured flags


def test_missing_and_healthy(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 1)
    q1, q2 = registry.evidence_status()
    assert q1.flags == [] and q1.sites == 2
    assert q2.flags == ["missing"]


def test_thin_counts_distinct_sites_not_pages(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://a.com/2", 1)
    status = registry.question_status(1)
    assert status.sources == 2 and status.sites == 1 and "thin" in status.flags


def test_outdated_and_undated(registry):
    add(registry, "https://a.com/1", 1, published="2026-01-01")  # > 365/2 days before TODAY
    add(registry, "https://b.com/2", 1, published="2025-12-01")
    assert registry.question_status(1).flags == ["outdated"]
    add(registry, "https://c.com/3", 2, published=None)
    add(registry, "https://d.com/4", 2, published=None)
    assert registry.question_status(2).flags == ["undated"]


def test_outdated_ignored_for_timeless_topics(registry):
    registry.plan.time_sensitive = False
    add(registry, "https://a.com/1", 1, published="2020-01-01")
    add(registry, "https://b.com/2", 1, published="2020-01-01")
    assert registry.question_status(1).flags == []


def test_weak_quotes(registry):
    add(registry, "https://a.com/1", 1, quote="invented")
    add(registry, "https://b.com/2", 1, quote="also invented")
    assert registry.question_status(1).flags == ["weak_quotes"]


def test_excluded_sources_do_not_count(registry):
    sid = add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 1)
    registry.sources[sid].excluded = True
    assert "thin" in registry.question_status(1).flags


# ---------------------------------------------------------------- record_gap / rounds


def test_record_gap_validates_input(registry):
    assert "not in the research plan" in registry.record_gap(9, "thin", WHY, ["q"])
    assert "Explain concretely" in registry.record_gap(1, "thin", "matters", ["q"])
    assert "follow-up search query" in registry.record_gap(1, "thin", WHY, [])
    assert registry.gaps == []


def test_measured_label_must_match_measurement(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 1)  # 2 sites, recent: not thin
    assert "not measured as thin" in registry.record_gap(1, "thin", WHY, ["q"])
    assert registry.record_gap(1, "missing_angle", WHY, ["q"]) is None  # judgement call OK


def test_record_gap_snapshots_before_and_targets_next_round(registry, events):
    add(registry, "https://a.com/1", 1)
    assert registry.record_gap(1, "thin", WHY, ["vendor-neutral cost study"]) is None
    gap = registry.gaps[0]
    assert gap.round == 2 and gap.before.sites == 1
    assert events[-1].kind == "gap" and WHY in events[-1].message


def test_round_advances_once_when_follow_up_starts(registry, events):
    add(registry, "https://a.com/1", 1)
    registry.record_gap(1, "thin", WHY, ["q"])
    registry.record_gap(2, "missing", WHY, ["q"])
    registry.start_pending_round()
    registry.start_pending_round()
    assert registry.round == 2
    assert [e.message for e in events if e.kind == "round"] == \
        ["Round 2 started: following up 2 gap(s) on Q1, Q2"]


def test_record_gap_refused_at_round_limit(registry):
    registry.round = registry.max_rounds
    assert "round limit" in registry.record_gap(1, "thin", WHY, ["q"])


# ---------------------------------------------------------------- the gap gate


def test_gate_rejects_flagged_questions_while_rounds_remain(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 2)
    problems = registry.check_report("A [S1]. B [S2].")
    assert len(problems) == 2
    assert problems[0].startswith("Q1 evidence is thin") and "record_gap" in problems[0]


def test_gate_rejects_recorded_but_unresearched_gaps(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 2)
    registry.record_gap(1, "thin", WHY, ["q"])
    problems = registry.check_report("A [S1]. B [S2].")
    assert problems == [("Gaps were recorded for Q1 but their follow-up research has not run. "
                        "Delegate the follow-ups to the researcher first.")]


def test_gate_accepts_clean_evidence_in_round_one(registry):
    for i, q in enumerate([1, 1, 2, 2]):
        add(registry, f"https://site{i}.com/x", q)
    assert registry.finalize("A [S1]. B [S3].") == []


def test_full_gap_cycle_report_shows_before_and_after(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 2)
    add(registry, "https://c.com/3", 2)
    registry.record_gap(1, "thin", WHY, ["independent cost estimate"])
    registry.start_pending_round()
    add(registry, "https://d.com/4", 1)

    assert registry.finalize("A [S1, S4]. B [S2].") == []
    report = registry.report
    assert "## How this report was strengthened" in report
    row = next(line for line in report.splitlines() if line.startswith("| Q1"))
    assert "| thin |" in row and WHY in row
    assert "1 source from 1 site" in row and "2 sources from 2 sites" in row
    assert "Remaining evidence gaps" not in report
    assert report.index("strengthened") < report.index("## Sources")


def test_at_round_limit_remaining_gaps_are_listed_not_blocking(registry):
    add(registry, "https://a.com/1", 1)
    add(registry, "https://b.com/2", 2)
    add(registry, "https://c.com/3", 2)
    registry.record_gap(1, "thin", WHY, ["q"])
    registry.start_pending_round()  # follow-up found nothing new

    assert registry.finalize("A [S1]. B [S2].") == []
    assert "| thin (still open) |" in registry.report
    assert "## Remaining evidence gaps" in registry.report
    assert "- **Q1** (Obligations?): thin" in registry.report


def test_assumptions_are_appended(registry):
    for i, q in enumerate([1, 1, 2, 2]):
        add(registry, f"https://site{i}.com/x", q)
    registry.assumptions.append("Assumed EU startups only.")
    registry.finalize("A [S1]. B [S3].")
    assert "## Assumptions\n\n- Assumed EU startups only." in registry.report
