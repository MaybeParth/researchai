"""Phase 0 smoke test: confirm Deep Agents + Claude work and print the stream chunk shapes.

Run: .venv/bin/python scripts/smoke_deepagents.py
Needs ANTHROPIC_API_KEY in .env. Makes a few cheap real API calls.
"""

from deepagents import create_deep_agent
from langchain_anthropic import ChatAnthropic

from researchai.config import get_settings

try:  # to-dos are opt-in in recent deepagents versions
    from langchain.agents.middleware import TodoListMiddleware
except ImportError:  # pragma: no cover
    TodoListMiddleware = None


def lookup_fact(topic: str) -> str:
    """Return a canned fact about a topic (stand-in for web search)."""
    return f"Fact about {topic}: it was widely discussed in 2026 [S1]."


def main() -> None:
    s = get_settings()
    lead = ChatAnthropic(model=s.lead_model, api_key=s.anthropic_api_key)
    worker = ChatAnthropic(model=s.researcher_model, api_key=s.anthropic_api_key)

    agent = create_deep_agent(
        model=lead,
        tools=[],
        system_prompt=(
            "Plan with write_todos (2 items), then delegate each item to the 'researcher' "
            "subagent via the task tool, then answer in two sentences."
        ),
        subagents=[
            {
                "name": "researcher",
                "description": "Looks up facts about a topic.",
                "system_prompt": "Use lookup_fact once and report the result in one sentence.",
                "tools": [lookup_fact],
                "model": worker,
            }
        ],
        middleware=[TodoListMiddleware()] if TodoListMiddleware else [],
    )

    print(f"TodoListMiddleware available: {TodoListMiddleware is not None}")
    for namespace, mode, data in agent.stream(
        {"messages": [{"role": "user", "content": "Quick facts: solid-state batteries"}]},
        stream_mode=["updates"],
        subgraphs=True,
    ):
        if mode != "updates" or not isinstance(data, dict):
            continue
        for node, update in data.items():
            keys = list(update.keys()) if isinstance(update, dict) else type(update).__name__
            print(f"[{'/'.join(namespace) or 'lead'}] node={node} keys={keys}")
            if isinstance(update, dict) and update.get("todos"):
                print("   todos:", [(t["content"], t["status"]) for t in update["todos"]])
            for msg in (update.get("messages") or []) if isinstance(update, dict) else []:
                for call in getattr(msg, "tool_calls", None) or []:
                    print(f"   tool_call: {call['name']} {list(call['args'])}")


if __name__ == "__main__":
    main()
