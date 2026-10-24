"""Web search providers. TavilySearch for real runs, FakeSearch for tests."""

import asyncio
from typing import Protocol

from tavily import AsyncTavilyClient


class SearchProvider(Protocol):
    async def search(self, query: str, *, recent: bool, horizon_days: int) -> list[dict]:
        """Return result dicts with url, title, content, score and optional published_date."""
        ...


def _time_range(horizon_days: int) -> str:
    if horizon_days <= 7:
        return "week"
    if horizon_days <= 31:
        return "month"
    return "year"


class TavilySearch:
    def __init__(self, api_key: str, max_results: int = 6):
        self.client = AsyncTavilyClient(api_key)
        self.max_results = max_results

    async def search(self, query: str, *, recent: bool, horizon_days: int) -> list[dict]:
        general = self.client.search(query, max_results=self.max_results)
        if not recent:
            return (await general)["results"]
        # General search rarely carries publish dates; news search does. For time-sensitive
        # topics run both so triage can judge recency while still finding official pages.
        news = self.client.search(
            query, topic="news", time_range=_time_range(horizon_days), max_results=self.max_results
        )
        g, n = await asyncio.gather(general, news)
        return n["results"] + g["results"]


class FakeSearch:
    """Returns canned results. `responses` maps a query substring to results."""

    def __init__(self, responses: dict[str, list[dict]] | None = None, default=None):
        self.responses = responses or {}
        self.default = default or []
        self.calls: list[str] = []

    async def search(self, query: str, *, recent: bool, horizon_days: int) -> list[dict]:
        self.calls.append(query)
        for key, results in self.responses.items():
            if key.lower() in query.lower():
                return results
        return self.default
