"""Evidence bookkeeping and the code-side guardrails.

Everything here is deterministic: no LLM or network calls. The agent decides what to
search and write; this module decides which sources are admissible, assigns citable IDs,
and verifies the final report's citations.
"""

import re
import threading
from collections import Counter
from collections.abc import Callable
from datetime import date
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from researchai.models import Event, Evidence, Gap, QuestionStatus, ResearchPlan, Source

MEASURED_PROBLEMS = {"missing", "thin", "outdated", "undated", "weak_quotes"}

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid"}
OFFICIAL_SUFFIXES = (".gov", ".edu", ".int", ".mil", "europa.eu", ".ac.uk", ".gov.uk")
ESTABLISHED_OUTLETS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "ft.com", "economist.com",
    "nytimes.com", "wsj.com", "bloomberg.com", "theguardian.com", "nature.com",
    "science.org", "arxiv.org", "techcrunch.com", "politico.eu", "who.int", "oecd.org",
}
LOW_QUALITY = {"pinterest.com", "quora.com", "answers.com", "scribd.com"}

MIN_RELEVANCE = 0.3  # search-engine relevance below this is treated as off-topic
CITATION_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")


# ---------------------------------------------------------------- pure helpers


def canonical_url(url: str) -> str:
    """Normalise a URL so the same article from different links compares equal."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def parse_date(value: str | None) -> date | None:
    """Accept RFC 2822 (Tavily news) or ISO dates; None if missing or unparseable."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def credibility(domain: str) -> tuple[float, str | None]:
    """Score adjustment and reason based on the publishing domain."""
    if domain.endswith(OFFICIAL_SUFFIXES):
        return 0.2, "official or academic source"
    if domain in ESTABLISHED_OUTLETS:
        return 0.1, "established outlet"
    if domain in LOW_QUALITY:
        return -0.3, "low-quality site"
    return 0.0, None


def triage(
    results: list[dict],
    plan: ResearchPlan | None,
    today: date,
    limit: int,
) -> list[Source]:
    """Score raw search results and decide keep/drop, recording a reason for each.

    Returns every result as a Source; kept ones have kept=True. Duplicates within the
    batch, stale sources (for time-sensitive topics) and off-topic results are dropped;
    the best `limit` of the remainder are kept.
    """
    seen: set[str] = set()
    candidates: list[Source] = []
    dropped: list[Source] = []

    for r in results:
        url = r.get("url") or ""
        if not url:
            continue
        src = Source(
            url=url,
            canonical_url=canonical_url(url),
            title=(r.get("title") or url).strip(),
            domain=domain_of(url),
            published=parse_date(r.get("published_date")),
            content=r.get("content") or "",
            score=float(r.get("score") or 0.0),
        )

        if src.canonical_url in seen:
            src.reasons.append("duplicate result in this search")
            dropped.append(src)
            continue
        seen.add(src.canonical_url)

        if src.score < MIN_RELEVANCE:
            src.reasons.append(f"low relevance ({src.score:.2f})")
            dropped.append(src)
            continue

        if src.published is None:
            src.score -= 0.15
            src.reasons.append("publish date unknown")
        else:
            age = (today - src.published).days
            if plan and plan.time_sensitive and age > plan.horizon_days:
                src.reasons.append(
                    f"published {src.published}, older than the {plan.horizon_days}-day window"
                )
                dropped.append(src)
                continue
            if age <= 90:
                src.score += 0.1
                src.reasons.append(f"recent ({src.published})")
            else:
                src.reasons.append(f"published {src.published}")

        delta, why = credibility(src.domain)
        src.score += delta
        if why:
            src.reasons.append(why)
        candidates.append(src)

    candidates.sort(key=lambda s: s.score, reverse=True)
    for i, src in enumerate(candidates):
        if i < limit:
            src.kept = True
        else:
            src.reasons.append("ranked below the kept results")
            dropped.append(src)
    return candidates[:limit] + dropped


def cited_ids(markdown: str) -> list[str]:
    """Source IDs cited in a report, in order of first appearance."""
    ids: list[str] = []
    for group in CITATION_RE.findall(markdown):
        for sid in re.split(r"\s*,\s*", group):
            if sid not in ids:
                ids.append(sid)
    return ids


def reason_bucket(reason: str) -> str:
    """Collapse a specific drop reason into a short category for log summaries."""
    if "older than" in reason:
        return "outdated"
    if reason.startswith("low relevance"):
        return "off-topic"
    if reason.startswith("duplicate"):
        return "duplicate"
    if reason.startswith("ranked below"):
        return "lower-ranked"
    return reason


def _normalise(text: str) -> str:
    text = text.lower().replace("…", " ").replace("...", " ")
    text = re.sub(r"[\"'“”‘’]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    q = _normalise(quote)
    return bool(q) and q in _normalise(text)


# ---------------------------------------------------------------- registry


class SourceRegistry:
    """Single source of truth for one research session's evidence.

    Thread-safe: researcher subagents run in parallel and may call in concurrently.
    """

    def __init__(
        self,
        on_event: Callable[[Event], None] | None = None,
        today: date | None = None,
        sources_per_query: int = 4,
        min_sites: int = 2,
        max_rounds: int = 2,
    ):
        self.on_event = on_event or (lambda e: None)
        self.today = today or date.today()
        self.sources_per_query = sources_per_query
        self.min_sites = min_sites
        self.max_rounds = max_rounds
        self.plan: ResearchPlan | None = None
        self.sources: dict[str, Source] = {}  # kept sources by ID
        self.dropped: list[Source] = []
        self.evidence: list[Evidence] = []
        self.round = 1  # current research round
        self.gaps: list[Gap] = []
        self.assumptions: list[str] = []
        self.direction_key: str | None = None  # the direction questions, once asked
        self.direction: dict[str, list[str] | str] | None = None  # the user's choices
        self.direction_assumed = False  # True if recommended options were used unanswered
        self.skipped: set[int] = set()  # sub-questions the user skipped
        self.report: str | None = None
        self.report_version = 0  # bumps on every accepted report (revisions included)
        self._by_url: dict[str, str] = {}  # canonical URL -> source ID
        self._lock = threading.Lock()

    def emit(self, kind: str, message: str, agent: str = "lead", **data) -> None:
        self.on_event(Event(kind=kind, message=message, agent=agent, data=data))

    # -- user overrides

    def skip_question(self, number: int) -> str | None:
        """Stop researching a sub-question. Returns an error message if not possible."""
        if self.plan is None or self.plan.question(number) is None:
            return f"Q{number} is not in the research plan."
        with self._lock:
            self.skipped.add(number)
        self.emit("control", f"User skipped Q{number}: {self.plan.question(number)}",
                  agent="user", action="skip", question_number=number)
        return None

    def exclude_source(self, source_id: str) -> str | None:
        """Exclude a source from evidence and citations. Returns an error message if unknown."""
        src = self.sources.get(source_id)
        if src is None:
            return f"Unknown source {source_id}."
        with self._lock:
            src.excluded = True
        self.emit("control", f"User excluded {source_id} ({src.domain})", agent="user",
                  action="exclude", source_id=source_id)
        return None

    # -- planning

    def set_plan(self, plan: ResearchPlan) -> None:
        with self._lock:
            self.plan = plan
        numbered = "; ".join(f"Q{i}: {q}" for i, q in enumerate(plan.sub_questions, 1))
        timing = f"time-sensitive, last {plan.horizon_days} days" if plan.time_sensitive else "timeless"
        self.emit("plan", f"{len(plan.sub_questions)} sub-questions ({timing}) — {numbered}",
                  plan=plan.model_dump())

    # -- sources

    def add_results(self, results: list[dict], question_number: int, query: str) -> list[Source]:
        """Triage search results; return the sources the agent may cite (new or already known)."""
        decided = triage(results, self.plan, self.today, self.sources_per_query)
        usable: list[Source] = []
        with self._lock:
            for src in decided:
                existing_id = self._by_url.get(src.canonical_url)
                if existing_id:
                    known = self.sources[existing_id]
                    if not known.excluded:
                        if question_number not in known.found_for:
                            known.found_for.append(question_number)
                        usable.append(known)
                    continue
                if not src.kept:
                    self.dropped.append(src)
                    continue
                src.id = f"S{len(self.sources) + 1}"
                src.found_for.append(question_number)
                self.sources[src.id] = src
                self._by_url[src.canonical_url] = src.id
                usable.append(src)

        self.emit("search", f'Q{question_number} "{query}": {len(results)} results, '
                  f"{len(usable)} usable", agent="researcher", query=query)
        for src in decided:
            if src.kept and src.id:
                self.emit("kept", f"{src.id} {src.domain} — {', '.join(src.reasons) or 'relevant'}",
                          agent="researcher", source_id=src.id, url=src.url)
        dropped = [s for s in decided if not s.kept]
        if dropped:
            # One summary line per search; per-source reasons stay in the event data.
            counts = Counter(reason_bucket(s.reasons[-1]) for s in dropped)
            summary = ", ".join(f"{n} {why}" for why, n in counts.most_common())
            self.emit("dropped", f"{len(dropped)} dropped: {summary}", agent="researcher",
                      sources=[{"domain": s.domain, "url": s.url, "reasons": s.reasons}
                               for s in dropped])
        return usable

    # -- evidence

    def add_evidence(self, ev: Evidence) -> str | None:
        """Store evidence; return an error message if it is not admissible."""
        with self._lock:
            src = self.sources.get(ev.source_id)
            if src is None:
                return f"Unknown source ID {ev.source_id}. Use only IDs returned by web_search."
            if src.excluded:
                return f"{ev.source_id} was excluded by the user; do not use it."
            if self.plan and self.plan.question(ev.question_number) is None:
                return f"Question number {ev.question_number} is not in the research plan."
            if ev.question_number in self.skipped:
                return f"Q{ev.question_number} was skipped by the user; stop researching it."
            for claim in ev.claims:
                claim.verified = quote_in_text(claim.quote, src.content)
            self.evidence.append(ev)

        verified = sum(c.verified for c in ev.claims)
        self.emit("evidence", f"{ev.source_id} → Q{ev.question_number}: {len(ev.claims)} claims "
                  f"({verified} quotes verified against source text)", agent="researcher",
                  source_id=ev.source_id, question_number=ev.question_number)
        return None

    def evidence_sources(self, question_number: int) -> set[str]:
        return {
            e.source_id
            for e in self.evidence
            if e.question_number == question_number and not self.sources[e.source_id].excluded
        }

    # -- evidence review and gaps

    def question_status(self, number: int) -> QuestionStatus:
        """Measure one sub-question's evidence. Flags are deterministic, not LLM-judged."""
        assert self.plan is not None
        evs = [e for e in self.evidence if e.question_number == number
               and not self.sources[e.source_id].excluded]
        srcs = [self.sources[sid] for sid in {e.source_id for e in evs}]
        dates = [s.published for s in srcs if s.published]
        newest = max(dates) if dates else None
        claims = [c for e in evs for c in e.claims]
        verified = sum(c.verified for c in claims)
        sites = len({s.domain for s in srcs})

        flags: list[str] = []
        if number in self.skipped:
            pass  # the user chose not to pursue it: nothing to flag
        elif not srcs:
            flags.append("missing")
        else:
            if sites < self.min_sites:
                flags.append("thin")
            if newest is None:
                flags.append("undated")
            elif self.plan.time_sensitive and \
                    (self.today - newest).days > self.plan.horizon_days // 2:
                flags.append("outdated")
            if claims and verified * 2 < len(claims):
                flags.append("weak_quotes")
        return QuestionStatus(
            number=number, question=self.plan.sub_questions[number - 1], sources=len(srcs),
            sites=sites, newest=newest, quotes_verified=verified, quotes_total=len(claims),
            flags=flags, skipped=number in self.skipped,
        )

    def question_statuses(self) -> list[QuestionStatus]:
        if self.plan is None:
            return []
        return [self.question_status(n) for n in range(1, len(self.plan.sub_questions) + 1)]

    def evidence_status(self) -> list[QuestionStatus]:
        """Review checkpoint: measure every sub-question and log the result."""
        statuses = self.question_statuses()
        if not statuses:
            return []
        flagged = [f"Q{s.number} {'/'.join(s.flags)}" for s in statuses if s.flags]
        skipped = [f"Q{s.number} skipped" for s in statuses if s.skipped]
        self.emit("status", f"Round {self.round}/{self.max_rounds} review: "
                  + (", ".join(flagged) if flagged else "all sub-questions adequately sourced")
                  + (f" ({', '.join(skipped)})" if skipped else ""),
                  statuses=[s.model_dump(mode="json") for s in statuses])
        return statuses

    def pending_gaps(self) -> list[Gap]:
        """Gaps recorded for the next round whose follow-up research has not started yet."""
        return [g for g in self.gaps
                if g.round > self.round and g.question_number not in self.skipped]

    def record_gap(self, question_number: int, problem: str, why_it_matters: str,
                   follow_up_queries: list[str]) -> str | None:
        """Record a gap to research in the next round; return an error if not allowed."""
        if self.plan is None or self.plan.question(question_number) is None:
            return f"Q{question_number} is not in the research plan."
        if question_number in self.skipped:
            return f"Q{question_number} was skipped by the user; do not research it further."
        if self.round >= self.max_rounds:
            return (f"Research round limit ({self.max_rounds}) reached. Write the report and "
                    "list what is still missing under Open questions.")
        if len(why_it_matters.strip()) < 15:
            return "Explain concretely why this gap matters for the report's conclusions."
        if not follow_up_queries:
            return "Give at least one specific follow-up search query."
        before = self.question_status(question_number)
        if problem in MEASURED_PROBLEMS and problem not in before.flags:
            # Measured labels must match the measurement; judgement calls use their own labels.
            return (f"Q{question_number} is not measured as {problem} ({before.summary()}). "
                    "If something specific is missing, use missing_angle; if sources "
                    "disagree, use conflicting.")
        gap = Gap(question_number=question_number, problem=problem,
                  why_it_matters=why_it_matters.strip(), follow_up_queries=follow_up_queries,
                  round=self.round + 1, before=before)
        with self._lock:
            self.gaps.append(gap)
        self.emit("gap", f"Q{question_number} {problem} — {gap.why_it_matters}",
                  question_number=question_number, problem=problem,
                  why_it_matters=gap.why_it_matters, follow_up_queries=follow_up_queries)
        return None

    def start_pending_round(self) -> None:
        """Advance to the next round when follow-up research for recorded gaps begins."""
        with self._lock:
            pending = self.pending_gaps()
            if not pending:
                return
            self.round += 1
        questions = ", ".join(sorted({f"Q{g.question_number}" for g in pending}))
        self.emit("round", f"Round {self.round} started: following up {len(pending)} gap(s) "
                  f"on {questions}")

    # -- report gate

    def check_report(self, markdown: str) -> list[str]:
        """Problems that block the report from being accepted (empty list = OK)."""
        if self.plan is None:
            return ["No research plan set. Call set_research_plan first."]
        if not self.evidence:
            return ["No evidence recorded yet. Delegate research before writing the report."]

        problems: list[str] = []
        ids = cited_ids(markdown)
        if not ids:
            problems.append("The report has no citations. Cite sources like [S1].")
        for sid in ids:
            src = self.sources.get(sid)
            if src is None:
                problems.append(f"[{sid}] does not exist. Remove it or cite a real source.")
            elif src.excluded:
                problems.append(f"[{sid}] was excluded by the user. Remove it.")

        cited = set(ids)
        for n, question in enumerate(self.plan.sub_questions, 1):
            available = self.evidence_sources(n)
            if available and not cited & available and n not in self.skipped:
                problems.append(
                    f'Q{n} ("{question}") has evidence from {", ".join(sorted(available))} '
                    "but none of it is cited."
                )
        return problems + self._gap_problems()

    def _gap_problems(self) -> list[str]:
        """The gap gate: weak evidence must be followed up while rounds remain."""
        pending = self.pending_gaps()
        if pending:
            qs = ", ".join(sorted({f"Q{g.question_number}" for g in pending}))
            return [(f"Gaps were recorded for {qs} but their follow-up research has not run. "
                    "Delegate the follow-ups to the researcher first.")]
        if self.round >= self.max_rounds:
            return []  # out of rounds: remaining gaps are reported, not blocking
        return [
            f"Q{s.number} evidence is {'/'.join(s.flags)} ({s.summary()}). Call record_gap "
            "explaining why it matters, then research it before writing the report."
            for s in self.evidence_status() if s.flags
        ]

    def finalize(self, markdown: str) -> list[str]:
        """Accept the report if it passes the gate; return the blocking problems otherwise."""
        problems = self.check_report(markdown)
        if problems:
            self.emit("report_rejected", f"Report rejected: {len(problems)} problem(s)",
                      problems=problems)
            return problems
        appendix = [self.direction_section(), self.strengthened_section(),
                    self.remaining_gaps_section(), self.assumptions_section(), self.sources_section(cited_ids(markdown))]
        self.report = markdown.rstrip() + "\n\n" + "\n".join(s for s in appendix if s)
        self.report_version += 1
        still_open = sum(1 for g in self.gaps if self._still_open(g))
        self.emit("report_accepted", f"Report accepted with {len(cited_ids(markdown))} cited "
                  f"sources; {len(self.gaps)} gap(s) followed up, {still_open} still open")
        return []

    def _still_open(self, gap: Gap) -> bool:
        """Only measured gaps can be verified as closed; judged gaps count as followed up."""
        return gap.problem in self.question_status(gap.question_number).flags

    def strengthened_section(self) -> str:
        if not self.gaps:
            return ""
        lines = ["## How this report was strengthened", "",
                 ("After a first pass, the agent reviewed its evidence and ran targeted follow-up "
                 "research where it was weak."), "",
                 "| Sub-question | Gap found | Why it mattered | Before | After |",
                 "|---|---|---|---|---|"]
        for g in sorted(self.gaps, key=lambda g: g.question_number):
            after = self.question_status(g.question_number)
            status = (" (skipped by user)" if after.skipped
                      else " (still open)" if self._still_open(g) else "")
            lines.append(f"| Q{g.question_number} | {g.problem.replace('_', ' ')}{status} | "
                         f"{g.why_it_matters} | {g.before.summary()} | {after.summary()} |")
        return "\n".join(lines) + "\n"

    def remaining_gaps_section(self) -> str:
        statuses = self.question_statuses()
        open_ = [s for s in statuses if s.flags]
        skipped = [s for s in statuses if s.skipped]
        if not open_ and not skipped:
            return ""
        lines = ["## Remaining evidence gaps", ""]
        lines += [f"- **Q{s.number}** ({s.question}): {', '.join(s.flags).replace('_', ' ')} "
                  f"— {s.summary()}" for s in open_]
        lines += [f"- **Q{s.number}** ({s.question}): skipped by the user, not researched "
                  "further" for s in skipped]
        return "\n".join(lines) + "\n"

    def direction_section(self) -> str:
        if not self.direction or self.direction_assumed:
            return ""  # unanswered directions are listed under Assumptions instead
        lines = ["## Research direction", "", "Chosen by you before the research started:", ""]
        for header, value in self.direction.items():
            text = value if isinstance(value, str) else ", ".join(value)
            lines.append(f"- **{header}:** {text}")
        return "\n".join(lines) + "\n"

    def assumptions_section(self) -> str:
        if not self.assumptions:
            return ""
        return "## Assumptions\n\n" + "\n".join(f"- {a}" for a in self.assumptions) + "\n"

    def sources_section(self, ids: list[str]) -> str:
        lines = ["## Sources", ""]
        for sid in sorted(ids, key=lambda s: int(s[1:])):
            src = self.sources[sid]
            when = src.published.isoformat() if src.published else "date unknown"
            lines.append(f"- **[{sid}]** [{src.title}]({src.url}) — {src.domain}, {when}")
        return "\n".join(lines) + "\n"
