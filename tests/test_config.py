from researchai.__main__ import main
from researchai.config import Settings


def test_missing_keys_reported():
    s = Settings(_env_file=None, anthropic_api_key="", tavily_api_key="x")
    assert s.missing_keys() == ["ANTHROPIC_API_KEY"]


def test_cli_help_without_topic(capsys):
    assert main([]) == 0
    assert "Research a topic" in capsys.readouterr().out
