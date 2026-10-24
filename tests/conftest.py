import os
import tempfile
from datetime import date

import pytest

# Keep the server module's SQLite store out of the repo during tests.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from researchai.models import ResearchPlan
from researchai.sources import SourceRegistry

TODAY = date(2026, 10, 22)


def result(url, title="T", content="Some content.", score=0.8, published=None):
    return {"url": url, "title": title, "content": content, "score": score,
            "published_date": published}


@pytest.fixture
def plan():
    return ResearchPlan(topic="EU AI Act", sub_questions=["Obligations?", "Costs?"],
                        time_sensitive=True, horizon_days=365)


@pytest.fixture
def events():
    return []


@pytest.fixture
def registry(plan, events):
    reg = SourceRegistry(on_event=events.append, today=TODAY, sources_per_query=3)
    reg.set_plan(plan)
    return reg
