"""Shared data shapes: research plan, sources, evidence, and log events."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ResearchPlan(BaseModel):
    topic: str
    sub_questions: list[str]
    time_sensitive: bool
    horizon_days: int = 365  # sources older than this are dropped when time_sensitive

    def question(self, number: int) -> str | None:
        """1-based lookup; None if out of range."""
        if 1 <= number <= len(self.sub_questions):
            return self.sub_questions[number - 1]
        return None


class DirectionOption(BaseModel):
    label: str = Field(description="1-6 words, e.g. 'Compliance costs'.")
    description: str = Field(description="One line: what choosing this changes in the research.")
    recommended: bool = False


class DirectionQuestion(BaseModel):
    header: str = Field(description="Short tab label, e.g. 'Focus', 'Depth', 'Timeframe'.")
    question: str
    options: list[DirectionOption] = Field(description="2-4 options.")
    multi_select: bool = Field(default=False, description="True if several options can apply.")


OWN_DIRECTION = "Your direction"  # answer key for the free-text direction the user writes


def validate_direction(questions: list[DirectionQuestion]) -> str | None:
    """Problems with a set of direction questions, or None if they are usable."""
    if not 1 <= len(questions) <= 3:
        return "Ask 1-3 questions."
    headers = [q.header.strip().lower() for q in questions]
    if len(set(headers)) != len(headers):
        return "Each question needs a different header."
    for q in questions:
        if not 2 <= len(q.options) <= 4:
            return f'"{q.header}" needs 2-4 options.'
        recommended = sum(o.recommended for o in q.options)
        if q.multi_select and recommended < 1:
            return f'"{q.header}" needs at least one recommended option.'
        if not q.multi_select and recommended != 1:
            return f'"{q.header}" needs exactly one recommended option.'
    return None


def recommended_answers(questions: list[dict]) -> dict[str, list[str]]:
    return {q["header"]: [o["label"] for o in q["options"] if o.get("recommended")]
            for q in questions}


def describe_answers(answers: dict[str, list[str] | str]) -> str:
    """'Focus: costs, investors · Depth: deep dive' — for the agent, log and report."""
    parts = []
    for header, value in answers.items():
        text = value if isinstance(value, str) else ", ".join(value)
        parts.append(f"{header}: {text}")
    return " · ".join(parts)


class Source(BaseModel):
    id: str | None = None  # "S1", "S2", ... assigned only to kept sources
    url: str
    canonical_url: str
    title: str
    domain: str
    published: date | None = None
    content: str = ""
    score: float = 0.0  # final triage score (search relevance + adjustments)
    kept: bool = False
    excluded: bool = False  # set by the user
    reasons: list[str] = Field(default_factory=list)
    found_for: list[int] = Field(default_factory=list)  # sub-question numbers


class Claim(BaseModel):
    text: str = Field(description="The factual claim, in your own words.")
    quote: str = Field(description="Exact supporting quote copied from the source text.")
    verified: bool = False  # quote found verbatim in the source text


class Evidence(BaseModel):
    source_id: str
    question_number: int
    summary: str
    takeaways: list[str]
    claims: list[Claim]


GapProblem = Literal[
    # measured in code by evidence_status
    "missing", "thin", "outdated", "undated", "weak_quotes",
    # judged by the lead agent
    "conflicting", "missing_angle",
]


class QuestionStatus(BaseModel):
    """Code-measured evidence state of one sub-question."""

    number: int
    question: str
    sources: int  # distinct sources with evidence
    sites: int  # distinct domains among them
    newest: date | None
    quotes_verified: int
    quotes_total: int
    flags: list[str]  # subset of the measured GapProblem values
    skipped: bool = False  # skipped by the user: never flagged

    def summary(self) -> str:
        newest = self.newest.isoformat() if self.newest else "n/a"
        return (f"{_count(self.sources, 'source')} from {_count(self.sites, 'site')}, "
                f"newest {newest}, quotes verified {self.quotes_verified}/{self.quotes_total}")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


class Gap(BaseModel):
    question_number: int
    problem: GapProblem
    why_it_matters: str
    follow_up_queries: list[str]
    round: int  # the research round that will address it
    before: QuestionStatus


EventKind = Literal[
    "plan",
    "todos",
    "delegate",
    "search",
    "kept",
    "dropped",
    "evidence",
    "status",
    "gap",
    "round",
    "question",
    "answer",
    "assumption",
    "report_rejected",
    "report_accepted",
    "control",  # a user action: pause, resume, skip, exclude
    "memory",  # related past research found
    "suggestions",  # follow-up topics suggested after the report
    "session",  # session status change
    "agent",
    "error",
]


class Event(BaseModel):
    kind: EventKind
    message: str
    agent: str = "lead"  # lead, researcher, user or system
    data: dict = Field(default_factory=dict)
    ts: datetime = Field(default_factory=datetime.now)
    seq: int = 0  # position in the session log, assigned by the session
