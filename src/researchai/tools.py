"""Agent tools. Thin wrappers that route every agent action through the SourceRegistry."""

import json
from dataclasses import dataclass

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from researchai.controls import Controls
from researchai.models import (
    Claim,
    DirectionQuestion,
    Evidence,
    GapProblem,
    ResearchPlan,
    describe_answers,
    validate_direction,
)
from researchai.search import SearchProvider
from researchai.sources import SourceRegistry

MAX_SNIPPET = 1200


class SetPlanArgs(BaseModel):
    topic: str = Field(description="The research topic as you understand it.")
    sub_questions: list[str] = Field(description="3-5 focused sub-questions, in order.")
    time_sensitive: bool = Field(
        description="True for news, policy, markets, fast-moving tech; False for history/concepts."
    )
    horizon_days: int = Field(
        default=365, description="For time-sensitive topics, max source age in days."
    )


class WebSearchArgs(BaseModel):
    query: str = Field(description="A specific search query.")
    question_number: int = Field(description="The sub-question number this search serves (Q1=1).")


class RecordEvidenceArgs(BaseModel):
    source_id: str = Field(description="Source ID from web_search, e.g. S3.")
    question_number: int = Field(description="Sub-question number this evidence answers.")
    summary: str = Field(description="2-3 sentence summary of what the source says.")
    takeaways: list[str] = Field(description="1-3 key takeaways.")
    claims: list[Claim] = Field(description="1-4 factual claims, each with an exact quote.")


class FinalizeArgs(BaseModel):
    markdown: str = Field(description="The full report in markdown, with [Sn] citations.")


class EvidenceStatusArgs(BaseModel):
    pass


class RecordGapArgs(BaseModel):
    question_number: int = Field(description="The sub-question with weak evidence.")
    problem: GapProblem = Field(
        description="missing/thin/outdated/undated/weak_quotes (as flagged by evidence_status), "
        "or your own judgement: conflicting (sources disagree) or missing_angle."
    )
    why_it_matters: str = Field(
        description="Concretely, what the report would get wrong or leave unsupported if this "
        "gap stayed open."
    )
    follow_up_queries: list[str] = Field(description="1-3 specific search queries to close it.")


class AskUserArgs(BaseModel):
    questions: list[DirectionQuestion] = Field(
        description="1-3 questions that let the user steer the research direction."
    )


@dataclass
class Tools:
    set_research_plan: BaseTool
    web_search: BaseTool
    record_evidence: BaseTool
    finalize_report: BaseTool
    evidence_status: BaseTool
    record_gap: BaseTool
    ask_user: BaseTool

    @property
    def lead(self) -> list[BaseTool]:
        return [self.ask_user, self.set_research_plan, self.evidence_status, self.record_gap,
                self.finalize_report]

    @property
    def researcher(self) -> list[BaseTool]:
        return [self.web_search, self.record_evidence]


def make_tools(
    registry: SourceRegistry,
    search: SearchProvider,
    controls: Controls | None = None,
    require_direction: bool = True,
) -> Tools:
    controls = controls or Controls()

    async def set_research_plan(
        topic: str, sub_questions: list[str], time_sensitive: bool, horizon_days: int = 365
    ) -> str:
        if require_direction and registry.direction is None:
            return ("Rejected: ask the user about the research direction first (ask_user), "
                    "then plan around their choices.")
        plan = ResearchPlan(topic=topic, sub_questions=sub_questions,
                            time_sensitive=time_sensitive, horizon_days=horizon_days)
        registry.set_plan(plan)
        numbered = "\n".join(f"Q{i}: {q}" for i, q in enumerate(sub_questions, 1))
        return f"Plan recorded.\n{numbered}"

    async def web_search(query: str, question_number: int) -> str:
        plan = registry.plan
        if plan is None:
            return "No research plan yet; the lead must call set_research_plan first."
        if plan.question(question_number) is None:
            return f"Q{question_number} is not in the plan (1-{len(plan.sub_questions)})."
        if question_number in registry.skipped:
            return (f"Q{question_number} was skipped by the user. Stop researching it and "
                    "report back what you have.")
        registry.start_pending_round()
        try:
            results = await search.search(
                query, recent=plan.time_sensitive, horizon_days=plan.horizon_days
            )
        except Exception as exc:  # noqa: BLE001 — API errors become agent feedback, not crashes
            registry.emit("error", f"Search failed for {query!r}: {exc}", agent="researcher")
            return f"Search failed: {exc}. Try a different query."

        usable = registry.add_results(results, question_number, query)
        if not usable:
            return "No usable sources (all were off-topic, outdated or duplicates). Rephrase."
        blocks = []
        for src in usable:
            when = src.published.isoformat() if src.published else "date unknown"
            blocks.append(f"[{src.id}] {src.title} ({src.domain}, {when})\n"
                          f"{src.content[:MAX_SNIPPET]}")
        return "\n\n".join(blocks)

    async def record_evidence(
        source_id: str, question_number: int, summary: str, takeaways: list[str],
        claims: list[Claim],
    ) -> str:
        ev = Evidence(source_id=source_id, question_number=question_number, summary=summary,
                      takeaways=takeaways,
                      claims=[Claim.model_validate(c) for c in claims])
        error = registry.add_evidence(ev)
        if error:
            return f"Rejected: {error}"
        unverified = [c.quote for c in ev.claims if not c.verified]
        note = (f" {len(unverified)} quote(s) were not found verbatim in the source text; "
                "copy quotes exactly." if unverified else "")
        return f"Recorded evidence from {source_id} for Q{question_number}.{note}"

    async def finalize_report(markdown: str) -> str:
        problems = registry.finalize(markdown)
        if problems:
            return "REJECTED. Fix these problems and call finalize_report again:\n- " + \
                "\n- ".join(problems)
        return "ACCEPTED. The report is complete; reply with a one-line confirmation."

    async def evidence_status() -> str:
        statuses = registry.evidence_status()
        if not statuses:
            return "No research plan yet."
        lines = [f"Research round {registry.round} of {registry.max_rounds}."]
        for s in statuses:
            if s.skipped:
                lines.append(f"Q{s.number}: skipped by the user — do not research or report on it")
                continue
            verdict = "FLAGGED: " + ", ".join(s.flags) if s.flags else "ok"
            lines.append(f"Q{s.number}: {verdict} — {s.summary()}")
        if registry.round >= registry.max_rounds:
            lines.append("No research rounds left: write the report and list remaining gaps "
                         "under Open questions.")
        elif any(s.flags for s in statuses):
            lines.append("For each flagged question call record_gap, then delegate follow-ups.")
        return "\n".join(lines)

    async def record_gap(question_number: int, problem: str, why_it_matters: str,
                         follow_up_queries: list[str]) -> str:
        error = registry.record_gap(question_number, problem, why_it_matters, follow_up_queries)
        if error:
            return f"Rejected: {error}"
        return (f"Gap recorded for Q{question_number}. Delegate a follow-up task to the "
                f"researcher using these queries: {'; '.join(follow_up_queries)}")

    async def ask_user(questions: list[DirectionQuestion]) -> str:
        qs = [DirectionQuestion.model_validate(q) for q in questions]
        problem = validate_direction(qs)
        if problem:
            return f"Rejected: {problem} Fix the questions and call ask_user again."
        payload = [q.model_dump() for q in qs]
        key = json.dumps(payload, sort_keys=True)
        # LangGraph re-runs this tool from the top when resuming after interrupt(),
        # so the "already asked" check and the event must be idempotent for the same questions.
        if registry.direction_key not in (None, key):
            return "You already asked about research direction. Plan with the answers you have."
        if registry.direction_key is None:
            registry.direction_key = key
            registry.emit("question", "Choose the direction for this research: "
                          + " · ".join(q.header for q in qs), questions=payload)
        reply = interrupt({"questions": payload})
        answers, assumed = reply["answers"], reply["assumed"]
        registry.direction, registry.direction_assumed = answers, assumed
        summary = describe_answers(answers)
        if assumed:
            registry.assumptions.append(f"No one was available to choose a direction, so the "
                                        f"agent used its recommended options: {summary}.")
            registry.emit("assumption", f"Using the recommended direction: {summary}",
                          answers=answers)
            return (f"Nobody could answer, so use your recommended options: {summary}. "
                    "Plan the research around them.")
        registry.emit("answer", f"You chose: {summary}", agent="user", answers=answers)
        return (f"The user chose this direction: {summary}. Plan the sub-questions around "
                "these choices; treat any text they wrote as a priority.")

    def tool(fn, schema: type[BaseModel], description: str) -> BaseTool:
        async def run(**kwargs):
            await controls.wait_if_paused()  # a paused session stops at its next tool call
            return await fn(**kwargs)

        return StructuredTool.from_function(coroutine=run, name=fn.__name__,
                                            args_schema=schema, description=description)

    return Tools(
        set_research_plan=tool(
            set_research_plan, SetPlanArgs,
            "Record the research plan (sub-questions, time sensitivity). Call after ask_user.",
        ),
        web_search=tool(
            web_search, WebSearchArgs,
            "Search the web. Returns filtered sources with citable IDs like [S3].",
        ),
        record_evidence=tool(
            record_evidence, RecordEvidenceArgs,
            "Record what one source says about one sub-question, with quoted claims.",
        ),
        finalize_report=tool(
            finalize_report, FinalizeArgs,
            "Submit the final markdown report. Citations and evidence gaps are checked; fix and "
            "resubmit if rejected. The only way to deliver it.",
        ),
        evidence_status=tool(
            evidence_status, EvidenceStatusArgs,
            "Measure the evidence for each sub-question and flag weak ones.",
        ),
        record_gap=tool(
            record_gap, RecordGapArgs,
            "Record an evidence gap, why it matters, and follow-up queries. Required before "
            "researching a sub-question again.",
        ),
        ask_user=tool(
            ask_user, AskUserArgs,
            "Ask the user to choose the research direction before planning: 1-3 questions "
            "(e.g. Focus, Depth, Timeframe), each with 2-4 options and a recommended choice. "
            "The user clicks options and may add their own direction. Call once, first.",
        ),
    )
