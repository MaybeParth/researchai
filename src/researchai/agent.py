"""Assembles the Deep Agent: a lead planner/writer plus a parallel researcher subagent."""

from datetime import date

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver

from researchai.config import Settings
from researchai.prompts import LEAD_PROMPT, RESEARCHER_PROMPT
from researchai.tools import Tools


def build_agent(
    tools: Tools,
    settings: Settings,
    lead_model: BaseChatModel | None = None,
    researcher_model: BaseChatModel | None = None,
    today: date | None = None,
):
    """Models can be injected for tests; by default Sonnet leads and Haiku researches."""
    today_str = (today or date.today()).isoformat()
    lead_model = lead_model or ChatAnthropic(
        model=settings.lead_model, api_key=settings.anthropic_api_key, max_tokens=8000
    )
    researcher_model = researcher_model or ChatAnthropic(
        model=settings.researcher_model, api_key=settings.anthropic_api_key, max_tokens=4000
    )
    return create_deep_agent(
        model=lead_model,
        tools=tools.lead,
        system_prompt=LEAD_PROMPT.format(today=today_str),
        subagents=[
            {
                "name": "researcher",
                "description": "Researches one numbered sub-question: searches the web, "
                "records quoted evidence with source IDs, and reports back.",
                "system_prompt": RESEARCHER_PROMPT.format(today=today_str),
                "tools": tools.researcher,
                "model": researcher_model,
            }
        ],
        middleware=[TodoListMiddleware()],
        checkpointer=MemorySaver(),
    )
