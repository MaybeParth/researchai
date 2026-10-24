"""Runs one research session, turns the agent's stream into log Events, and applies user
controls (pause, resume, skip, exclude, answer) while it runs."""

import asyncio
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.types import Command

from researchai.agent import build_agent
from researchai.config import Settings
from researchai.controls import Controls
from researchai.memory import Store, Suggester, related_research_note
from researchai.models import OWN_DIRECTION, Event, recommended_answers
from researchai.search import SearchProvider, TavilySearch
from researchai.sources import SourceRegistry, cited_ids
from researchai.tools import make_tools

# Given {"questions": [...]}, return the user's choices {header: [labels] | text} (None = no one).
AnswerProvider = Callable[[dict], Awaitable[dict | None]]
Status = Literal["starting", "running", "waiting_for_answer", "revising", "done", "failed"]


class ResearchSession:
    def __init__(
        self,
        topic: str,
        settings: Settings,
        on_event: Callable[[Event], None] = lambda e: None,
        answer_provider: AnswerProvider | None = None,
        search: SearchProvider | None = None,
        lead_model: BaseChatModel | None = None,
        researcher_model: BaseChatModel | None = None,
        store: Store | None = None,
        suggester: Suggester | None = None,
    ):
        self.id = uuid.uuid4().hex[:12]
        self.topic = topic
        self.created = datetime.now()
        self.status: Status = "starting"
        self.events: list[Event] = []
        self.todos: list[dict] = []
        self.pending_question: dict | None = None
        self.store = store
        self.suggester = suggester
        self.related: list[dict] = []  # past sessions on similar topics
        self.suggestions: list[dict] = []  # follow-up topics
        self._on_event = on_event
        self._subscribers: list[asyncio.Queue[Event]] = []
        self.answer_provider = answer_provider
        self._answer: asyncio.Future[dict] | None = None
        self.controls = Controls()
        self._turn_lock = asyncio.Lock()  # one agent turn at a time (run, then revisions)
        self.registry = SourceRegistry(
            on_event=self._record,
            sources_per_query=settings.sources_per_query,
            min_sites=settings.min_sources_per_question,
            max_rounds=settings.max_rounds,
        )
        search = search or TavilySearch(settings.tavily_api_key)
        self.agent = build_agent(
            make_tools(self.registry, search, self.controls,
                       require_direction=settings.ask_direction), settings,
            lead_model=lead_model, researcher_model=researcher_model,
        )
        self.config = {"configurable": {"thread_id": self.id}, "recursion_limit": 250}

    # ------------------------------------------------------------ events

    def _record(self, event: Event) -> None:
        event.seq = len(self.events) + 1
        self.events.append(event)
        self._on_event(event)
        for queue in self._subscribers:
            queue.put_nowait(event)

    def _emit(self, kind: str, message: str, agent: str = "lead", **data) -> None:
        self._record(Event(kind=kind, message=message, agent=agent, data=data))

    def subscribe(self) -> tuple[list[Event], asyncio.Queue[Event]]:
        """Backlog plus a queue of future events (taken atomically, so nothing is missed)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(queue)
        return list(self.events), queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _set_status(self, status: Status, message: str) -> None:
        self.status = status
        self._emit("session", message, agent="system", status=status)

    # ------------------------------------------------------------ running the agent

    async def run(self) -> str | None:
        """Run the agent to completion; returns the accepted report, or None."""
        content = f"Research topic: {self.topic}"
        if self.store:
            self.related = self.store.find_related(self.topic, exclude_id=self.id)
        if self.related:
            listed = "; ".join(f'"{r["topic"]}" ({r["created"][:10]})' for r in self.related)
            self._emit("memory", f"You've researched related topics before: {listed}. "
                       "Building on them and focusing on what's new.", related=self.related)
            content += "\n\n" + related_research_note(self.related)

        await self._turn({"messages": [{"role": "user", "content": content}]},
                         "running", "Research started")
        await self.revise_if_needed()  # a source may have been excluded during the final step
        if self.registry.report is None and self.status != "failed":
            self._emit("error", "The agent finished without an accepted report.")
        if self.registry.report:
            await self._suggest_topics()
        self.save()
        return self.registry.report

    async def revise(self, instruction: str) -> None:
        """Ask the agent to revise its accepted report (same conversation thread)."""
        await self._turn({"messages": [{"role": "user", "content": instruction}]},
                         "revising", "Revising the report")
        self.save()

    async def _suggest_topics(self) -> None:
        if self.suggester is None:
            return
        past = [s["topic"] for s in self.store.list() if s["id"] != self.id] if self.store else []
        try:
            self.suggestions = await self.suggester(self.topic, self.registry.report, past[:15])
        except Exception as exc:  # noqa: BLE001 — suggestions are optional, never fatal
            self._emit("error", f"Could not suggest related topics: {exc}", agent="system")
            return
        self._emit("suggestions", "Related topics you might explore next: "
                   + "; ".join(s["topic"] for s in self.suggestions), agent="lead",
                   suggestions=self.suggestions)

    def save(self) -> None:
        if self.store:
            self.store.save(self.snapshot(), [e.model_dump(mode="json") for e in self.events])

    async def _turn(self, stream_input, status: Status, message: str) -> None:
        async with self._turn_lock:
            self._set_status(status, message)
            try:
                await self._drive(stream_input)
            except Exception as exc:  # noqa: BLE001 — surface any agent failure in the log
                self._emit("error", f"Agent failed: {type(exc).__name__}: {exc}")
                self._set_status("failed", "Research failed")
                return
            self._set_status("done", "Research finished" if self.registry.report
                             else "Finished without a report")

    async def _drive(self, stream_input) -> None:
        while True:
            pending = None
            async for namespace, mode, data in self.agent.astream(
                stream_input, self.config, stream_mode=["updates"], subgraphs=True
            ):
                if mode != "updates" or not isinstance(data, dict):
                    continue
                if "__interrupt__" in data:  # ask_user paused the agent
                    pending = data["__interrupt__"][0].value
                    continue
                self._handle_update("researcher" if namespace else "lead", data)
            if pending is None:
                return
            stream_input = Command(resume=await self._resolve_question(pending))

    async def _resolve_question(self, payload: dict) -> dict:
        """Wait for the user's direction choices (no timeout). Without anyone to ask
        (non-interactive CLI, scripts), use the agent's recommended options."""
        previous = self.status
        self.pending_question = payload
        self._set_status("waiting_for_answer", "Waiting for you to choose a direction")
        try:
            answers = await self.answer_provider(payload) if self.answer_provider else None
        finally:
            self.pending_question = None
            self._set_status(previous, "Research resumed")
        if answers:
            return {"answers": answers, "assumed": False}
        return {"answers": recommended_answers(payload["questions"]), "assumed": True}

    async def await_submitted_answer(self, payload: dict) -> dict | None:
        """AnswerProvider for the web UI: waits until submit_answer() is called."""
        self._answer = asyncio.get_running_loop().create_future()
        try:
            return await self._answer
        finally:
            self._answer = None

    def _handle_update(self, agent: str, data: dict) -> None:
        for update in data.values():
            if not isinstance(update, dict):  # middleware nodes emit None
                continue
            todos = update.get("todos")
            if isinstance(todos, list) and todos and agent == "lead":
                self.todos = todos
                summary = ", ".join(f"{t['content']} [{t['status']}]" for t in todos)
                self._emit("todos", summary, agent=agent, todos=todos)
            messages = update.get("messages")
            if not isinstance(messages, list):
                continue
            for msg in messages:
                if not isinstance(msg, AIMessage):
                    continue
                for call in msg.tool_calls:
                    if call["name"] == "task":
                        desc = call["args"].get("description", "")
                        self._emit("delegate", f"→ {call['args'].get('subagent_type', 'subagent')}"
                                   f": {desc[:160]}", agent=agent)
                if not msg.tool_calls and agent == "lead" and msg.text.strip():
                    self._emit("agent", msg.text.strip()[:300], agent=agent)

    # ------------------------------------------------------------ user controls

    def pause(self) -> None:
        self.controls.pause()
        self._emit("control", "User paused the agent (it stops at its next step)", agent="user",
                   action="pause")

    def resume(self) -> None:
        self.controls.resume()
        self._emit("control", "User resumed the agent", agent="user", action="resume")

    def skip(self, question_number: int) -> str | None:
        return self.registry.skip_question(question_number)

    def exclude(self, source_id: str) -> str | None:
        """Exclude a source. If the accepted report cites it, call revise_if_needed()."""
        return self.registry.exclude_source(source_id)

    def revision_needed(self) -> str | None:
        """Instruction for the agent if the accepted report cites an excluded source."""
        reg = self.registry
        if reg.report is None:
            return None
        bad = [sid for sid in cited_ids(reg.report) if reg.sources[sid].excluded]
        if not bad:
            return None
        ids = ", ".join(bad)
        return (f"The user excluded source(s) {ids}. Revise the report so it no longer relies "
                f"on {ids}: remove those citations, drop or re-support the affected claims with "
                "the remaining sources, keep everything else, and resubmit with finalize_report.")

    async def revise_if_needed(self) -> None:
        instruction = self.revision_needed()
        if instruction:
            await self.revise(instruction)

    def submit_answer(self, answers: dict[str, list[str] | str]) -> str | None:
        """Deliver the user's direction choices. Returns an error message if not accepted."""
        if self._answer is None or self._answer.done() or not self.pending_question:
            return "No question is waiting for an answer"
        headers = {q["header"] for q in self.pending_question["questions"]}
        allowed = headers | {OWN_DIRECTION}
        cleaned = {}
        for header, value in answers.items():
            if header not in allowed:
                return f"Unknown question: {header}"
            if isinstance(value, str):
                value = value.strip()
            else:
                value = [v.strip() for v in value if v and v.strip()]
            if value:
                cleaned[header] = value
        if not cleaned:
            return "Choose at least one option or write your own direction"
        self._answer.set_result(cleaned)
        return None

    # ------------------------------------------------------------ state for the UI

    def snapshot(self) -> dict:
        reg = self.registry
        evidence_count = Counter(e.source_id for e in reg.evidence)
        cited = set(cited_ids(reg.report)) if reg.report else set()
        sources = [
            {
                "id": s.id, "title": s.title, "url": s.url, "domain": s.domain,
                "published": s.published.isoformat() if s.published else None,
                "reasons": s.reasons, "excluded": s.excluded, "questions": s.found_for,
                "evidence": evidence_count.get(s.id, 0), "cited": s.id in cited,
            }
            for s in reg.sources.values()
        ]
        return {
            "id": self.id, "topic": self.topic, "status": self.status,
            "created": self.created.isoformat(timespec="seconds"),
            "paused": self.controls.paused, "round": reg.round, "max_rounds": reg.max_rounds,
            "plan": reg.plan.model_dump() if reg.plan else None,
            "questions": [s.model_dump(mode="json") for s in reg.question_statuses()],
            "todos": self.todos,
            "gaps": [g.model_dump(mode="json") for g in reg.gaps],
            "sources": sources, "dropped": len(reg.dropped),
            "pending_question": self.pending_question,
            "assumptions": reg.assumptions,
            "report": reg.report, "report_version": reg.report_version,
            "related": self.related, "suggestions": self.suggestions, "archived": False,
            "direction": reg.direction, "direction_assumed": reg.direction_assumed,
        }


class ArchivedSession:
    """A finished session loaded from the store: read-only, replays its saved action log."""

    def __init__(self, record: dict):
        snap = record["snapshot"]
        self.id, self.topic = snap["id"], snap["topic"]
        self.created = datetime.fromisoformat(snap["created"])
        self.status = snap["status"]
        self._snapshot = {**snap, "archived": True, "paused": False, "pending_question": None}
        self.events = [Event.model_validate(e) for e in record["events"]]

    def snapshot(self) -> dict:
        return self._snapshot

    def subscribe(self) -> tuple[list[Event], asyncio.Queue[Event]]:
        return list(self.events), asyncio.Queue()  # nothing new will arrive

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        pass
