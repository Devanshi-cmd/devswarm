import pytest

from devswarm.config import (
    DEFAULT_MODEL,
    DevSwarmConfig,
    get_secret,
    parse_agent_overrides,
    parse_effort_overrides,
)


def test_all_agents_default_to_haiku_at_medium_effort() -> None:
    config = DevSwarmConfig()
    for name in ("planner", "coder", "tester", "reviewer"):
        assert config.for_agent(name).model == DEFAULT_MODEL  # type: ignore[arg-type]
        assert config.for_agent(name).effort == "medium"  # type: ignore[arg-type]


def test_overrides_touch_only_the_named_agent() -> None:
    config = DevSwarmConfig().with_overrides(
        parse_agent_overrides(["reviewer=claude-sonnet-5-5"]),
        parse_effort_overrides(["coder=high"]),
    )
    assert config.reviewer.model == "claude-sonnet-5-5"
    assert config.reviewer.max_turns == 10
    assert config.coder.effort == "high"
    assert config.coder.model == DEFAULT_MODEL
    assert config.planner.effort == "medium"


def test_model_without_a_price_is_rejected() -> None:
    with pytest.raises(ValueError, match="no price table entry"):
        DevSwarmConfig().with_overrides({"coder": "claude-imaginary"}, {})


@pytest.mark.parametrize("pair", ["boss=claude-haiku-5-5", "reviewer", "reviewer="])
def test_malformed_overrides_are_rejected(pair: str) -> None:
    with pytest.raises(ValueError, match="expected <agent>=<value>"):
        parse_agent_overrides([pair])


def test_unknown_effort_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown effort"):
        parse_effort_overrides(["coder=extreme"])


def test_placeholder_secret_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "your-github-token-here")
    assert get_secret("GITHUB_TOKEN") is None


def test_real_secret_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "abc123456")
    assert get_secret("GITHUB_TOKEN") == "abc123456"
