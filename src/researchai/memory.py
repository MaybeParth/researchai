"""Long-term memory: past sessions in SQLite, related-research lookup, topic suggestions.

Deliberately simple: keyword overlap instead of embeddings, one table, one small LLM call.
"""

import json
import re
import sqlite3
from collections.abc import Awaitable, Callable

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from researchai.config import Settings

STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "about", "what", "how", "why", "does", "are",
    "its", "this", "that", "their", "impact", "impacts", "effect", "effects", "role", "state",
    "current", "latest", "new", "vs", "versus", "between", "on", "of", "in", "to", "a", "an",
}


def keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower())
    return {w.rstrip("s") if len(w) > 4 else w for w in words if w not in STOPWORDS}


def key_takeaways(report: str | None, limit: int = 4) -> list[str]:
    """Bullets of the report's 'Key takeaways' section, without citations or bold markers."""
    if not report:
        return []
    match = re.search(r"^## Key takeaways\s*$(.*?)(?=^## |\Z)", report, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    bullets = [line[2:].strip() for line in match.group(1).splitlines() if line.startswith("- ")]
    clean = [re.sub(r"\s*\[S\d+(?:\s*,\s*S\d+)*\]", "", b).replace("**", "") for b in bullets]
    return clean[:limit]


class Store:
    """SQLite store of finished sessions: snapshot (incl. report) and the full action log."""

    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, topic TEXT NOT NULL, "
                "created TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL, "
                "events TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def save(self, snapshot: dict, events: list[dict]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot["id"], snapshot["topic"], snapshot["created"], snapshot["status"],
                 json.dumps(snapshot), json.dumps(events)),
            )

    def load(self, session_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT snapshot, events FROM sessions WHERE id = ?",
                             (session_id,)).fetchone()
        if row is None:
            return None
        return {"snapshot": json.loads(row[0]), "events": json.loads(row[1])}

    def delete(self, session_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0

    def list(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT id, topic, created, status FROM sessions "
                              "ORDER BY created DESC").fetchall()
        return [dict(zip(("id", "topic", "created", "status"), r, strict=True)) for r in rows]

    def find_related(self, topic: str, exclude_id: str | None = None, limit: int = 3) -> list[dict]:
        """Past sessions with a report whose topic or sub-questions share keywords with `topic`."""
        wanted = keywords(topic)
        if not wanted:
            return []
        with self._connect() as db:
            rows = db.execute("SELECT id, snapshot FROM sessions").fetchall()
        scored = []
        for sid, raw in rows:
            snap = json.loads(raw)
            if sid == exclude_id or not snap.get("report"):
                continue
            plan = snap.get("plan") or {}
            text = " ".join([snap["topic"], *plan.get("sub_questions", [])])
            shared = wanted & keywords(text)
            if len(shared) >= 2 or (shared and len(shared) / len(wanted) >= 0.5):
                scored.append((len(shared), snap["created"], {
                    "id": sid, "topic": snap["topic"], "created": snap["created"],
                    "takeaways": key_takeaways(snap["report"]), "shared": sorted(shared),
                }))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [item for *_, item in scored[:limit]]


def related_research_note(related: list[dict]) -> str:
    """Context for the lead agent's first message."""
    lines = [("Previous related research by this user (for context; build on it and focus on "
             "what is new or still open — cite only sources you find in this session):")]
    for r in related:
        lines.append(f'- "{r["topic"]}" ({r["created"][:10]})')
        lines += [f"    - {t}" for t in r["takeaways"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- topic suggestions

Suggester = Callable[[str, str, list[str]], Awaitable[list[dict]]]


class Suggestion(BaseModel):
    topic: str = Field(description="A specific research topic, phrased as a user would type it.")
    why: str = Field(description="One sentence on why it follows from this report.")


class Suggestions(BaseModel):
    suggestions: list[Suggestion] = Field(description="Exactly 3 suggestions.")


SUGGEST_PROMPT = """\
A user just received this research report on "{topic}".

Their earlier research topics: {past}

Suggest 3 follow-up research topics that would be genuinely useful next: build on the \
report's open questions and disagreements, or connect to their earlier interests. Do not \
repeat topics already researched.

Report:
{report}
"""


def make_suggester(settings: Settings) -> Suggester:
    model = ChatAnthropic(model=settings.researcher_model, api_key=settings.anthropic_api_key,
                          max_tokens=1000).with_structured_output(Suggestions)

    async def suggest(topic: str, report: str, past_topics: list[str]) -> list[dict]:
        result = await model.ainvoke(SUGGEST_PROMPT.format(
            topic=topic, past="; ".join(past_topics) or "none", report=report[:12000]))
        return [s.model_dump() for s in result.suggestions[:3]]

    return suggest
