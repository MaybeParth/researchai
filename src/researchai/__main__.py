"""CLI entry point: python -m researchai "topic"."""

import argparse
import asyncio
import sys
from pathlib import Path

from researchai.config import get_settings
from researchai.models import OWN_DIRECTION, Event

KIND_LABELS = {
    "plan": "PLAN", "todos": "TODOS", "delegate": "DELEGATE", "search": "SEARCH",
    "kept": "  kept", "dropped": "  dropped", "evidence": "EVIDENCE", "status": "REVIEW",
    "gap": ">> GAP", "round": ">> ROUND", "question": "QUESTION", "answer": "ANSWER",
    "assumption": "ASSUMED", "report_rejected": "REJECTED", "report_accepted": "ACCEPTED",
    "agent": "AGENT", "error": "ERROR", "memory": "MEMORY", "suggestions": "SUGGEST",
    "session": "SESSION", "control": "USER",
}
INDENT = " " * 35


def print_event(event: Event) -> None:
    label = KIND_LABELS.get(event.kind, event.kind.upper())
    print(f"{event.ts:%H:%M:%S} [{event.agent:<10}] {label:<10} {event.message}", flush=True)
    for problem in event.data.get("problems", []):
        print(f"{INDENT}- {problem}", flush=True)
    for query in event.data.get("follow_up_queries", []):
        print(f"{INDENT}follow-up: {query}", flush=True)
    for s in event.data.get("suggestions", []):
        print(f"{INDENT}- {s['topic']} — {s['why']}", flush=True)


async def read_line(prompt: str) -> str:
    """Read one line from stdin without blocking the event loop."""
    print(prompt, end="", flush=True)
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[str] = loop.create_future()

    def on_input() -> None:
        if not answered.done():
            answered.set_result(sys.stdin.readline().strip())

    loop.add_reader(sys.stdin, on_input)
    try:
        return await answered
    finally:
        loop.remove_reader(sys.stdin)


def parse_choice(text: str, question: dict) -> list[str]:
    """'1,3' -> those option labels; Enter -> the recommended options."""
    options = question["options"]
    if not text:
        return [o["label"] for o in options if o.get("recommended")]
    picks = [int(t) for t in text.replace(" ", "").split(",") if t.isdigit()]
    labels = [options[i - 1]["label"] for i in picks if 1 <= i <= len(options)]
    return labels if question.get("multi_select") else labels[:1]


async def ask_in_terminal(payload: dict) -> dict:
    """Ask each direction question in turn, then an optional free-text direction."""
    answers: dict[str, list[str] | str] = {}
    for q in payload["questions"]:
        print(f"\n{q['header']} — {q['question']}")
        for i, o in enumerate(q["options"], 1):
            tag = "  (recommended)" if o.get("recommended") else ""
            print(f"  {i}. {o['label']}{tag} — {o['description']}")
        how = "numbers, comma-separated" if q.get("multi_select") else "a number"
        answers[q["header"]] = parse_choice(await read_line(f"  Choose {how} (Enter = recommended): "), q)
    own = await read_line("\nYour own direction (optional, Enter to skip): ")
    if own:
        answers[OWN_DIRECTION] = own
    print()
    return answers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="researchai",
        description="Research a topic and write a cited report.",
    )
    parser.add_argument("topic", nargs="?", help="what to research")
    parser.add_argument("-o", "--output", default="report.md", help="report file path")
    args = parser.parse_args(argv)

    if not args.topic:
        parser.print_help()
        return 0

    settings = get_settings()
    missing = settings.missing_keys()
    if missing:
        print(f"Missing keys in .env: {', '.join(missing)}", file=sys.stderr)
        return 1

    # heavy imports only when researching
    from researchai.memory import Store, make_suggester
    from researchai.session import ResearchSession

    session = ResearchSession(
        args.topic, settings, on_event=print_event,
        answer_provider=ask_in_terminal if sys.stdin.isatty() else None,
        store=Store(settings.db_path), suggester=make_suggester(settings),
    )
    report = asyncio.run(session.run())
    if report is None:
        print("No report was produced.", file=sys.stderr)
        return 1

    from researchai.summary import build_summary, summary_markdown

    output = Path(args.output)
    output.write_text(report)
    summary_path = output.with_suffix(".summary.md")
    summary = build_summary(session.snapshot(), [e.model_dump(mode="json") for e in session.events])
    summary_path.write_text(summary_markdown(summary))
    reg = session.registry
    print(f"\nReport written to {output} and summary to {summary_path} — {len(reg.sources)} "
          f"sources kept, {len(reg.dropped)} dropped, {len(reg.evidence)} evidence records.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
