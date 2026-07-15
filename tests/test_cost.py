import json
import os

from server import cost


def _line(model, input_tokens=0, output_tokens=0, cache_write=0, cache_read=0):
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


# --- summarize_usage: single model ------------------------------------------

def test_summarize_usage_single_model_sums_tokens_and_cost():
    lines = [
        _line("claude-sonnet-5", input_tokens=1000, output_tokens=500),
        _line("claude-sonnet-5", input_tokens=2000, output_tokens=1000),
    ]
    result = cost.summarize_usage(lines)
    assert result["input_tokens"] == 3000
    assert result["output_tokens"] == 1500
    assert result["cache_read_tokens"] == 0
    assert result["cache_write_tokens"] == 0
    assert result["total_tokens"] == 4500
    assert result["messages"] == 2
    rates = cost.PRICES["sonnet"]
    expected = (3000 * rates["input"] + 1500 * rates["output"]) / 1_000_000.0
    assert result["cost_usd"] == round(expected, 4)
    assert set(result["by_model"]) == {"claude-sonnet-5"}
    assert result["by_model"]["claude-sonnet-5"]["messages"] == 2


def test_summarize_usage_prices_by_substring_tier():
    # "claude-fable-5" -> fable tier, "claude-haiku-4-5-20251001" -> haiku tier.
    lines = [
        _line("claude-fable-5", input_tokens=100, output_tokens=100),
        _line("claude-haiku-4-5-20251001", input_tokens=100, output_tokens=100),
    ]
    result = cost.summarize_usage(lines)
    fable_cost = result["by_model"]["claude-fable-5"]["cost_usd"]
    haiku_cost = result["by_model"]["claude-haiku-4-5-20251001"]["cost_usd"]
    assert fable_cost > haiku_cost > 0


def test_summarize_usage_unknown_model_uses_default_tier():
    lines = [_line("some-future-model", input_tokens=1_000_000, output_tokens=0)]
    result = cost.summarize_usage(lines)
    assert result["cost_usd"] == round(cost.PRICES["default"]["input"], 4)


# --- summarize_usage: mixed models ------------------------------------------

def test_summarize_usage_mixed_models_prices_each_message_with_its_own_model():
    lines = [
        _line("claude-opus-4-8", input_tokens=1_000_000, output_tokens=0),
        _line("claude-haiku-4-5-20251001", input_tokens=1_000_000, output_tokens=0),
    ]
    result = cost.summarize_usage(lines)
    expected = cost.PRICES["opus"]["input"] + cost.PRICES["haiku"]["input"]
    assert result["cost_usd"] == round(expected, 4)
    assert set(result["by_model"]) == {"claude-opus-4-8", "claude-haiku-4-5-20251001"}
    assert result["messages"] == 2
    assert result["input_tokens"] == 2_000_000


# --- summarize_usage: missing/malformed usage -------------------------------

def test_summarize_usage_missing_usage_keys_default_to_zero():
    lines = [{"type": "assistant", "message": {"model": "claude-sonnet-5", "usage": {}}}]
    result = cost.summarize_usage(lines)
    assert result["total_tokens"] == 0
    assert result["cost_usd"] == 0.0
    assert result["messages"] == 1


def test_summarize_usage_skips_non_assistant_and_malformed_lines():
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": "not-a-dict"},
        {"type": "assistant"},  # no message key at all
        {"type": "assistant", "message": {"model": "claude-sonnet-5"}},  # no usage
        {"type": "assistant", "message": {"model": "claude-sonnet-5", "usage": "nope"}},
        "not-a-dict-either",
        _line("claude-sonnet-5", input_tokens=10, output_tokens=5),
    ]
    result = cost.summarize_usage(lines)
    assert result["messages"] == 1
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5


def test_summarize_usage_empty_input_returns_zeros():
    result = cost.summarize_usage([])
    assert result == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "total_tokens": 0, "cost_usd": 0.0,
        "by_model": {}, "messages": 0,
    }


def test_summarize_usage_includes_cache_tokens():
    lines = [_line("claude-sonnet-5", input_tokens=100, output_tokens=50,
                   cache_write=200, cache_read=300)]
    result = cost.summarize_usage(lines)
    assert result["cache_write_tokens"] == 200
    assert result["cache_read_tokens"] == 300
    assert result["total_tokens"] == 100 + 50 + 200 + 300
    rates = cost.PRICES["sonnet"]
    expected = (100 * rates["input"] + 50 * rates["output"]
                + 200 * rates["cache_write"] + 300 * rates["cache_read"]) / 1_000_000.0
    assert result["cost_usd"] == round(expected, 4)


# --- price override via env -------------------------------------------------

def test_price_override_env_changes_cost(monkeypatch):
    monkeypatch.setenv("VOXA_PRICE_OVERRIDE_JSON",
                       json.dumps({"sonnet": {"input": 1000.0}}))
    lines = [_line("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)]
    result = cost.summarize_usage(lines)
    assert result["cost_usd"] == 1000.0


def test_price_override_env_merges_not_replaces(monkeypatch):
    # Overriding just "input" for sonnet must leave "output" at its built-in rate.
    monkeypatch.setenv("VOXA_PRICE_OVERRIDE_JSON",
                       json.dumps({"sonnet": {"input": 1000.0}}))
    lines = [_line("claude-sonnet-5", input_tokens=0, output_tokens=1_000_000)]
    result = cost.summarize_usage(lines)
    assert result["cost_usd"] == round(cost.PRICES["sonnet"]["output"], 4)


def test_price_override_env_can_add_a_new_tier(monkeypatch):
    monkeypatch.setenv("VOXA_PRICE_OVERRIDE_JSON",
                       json.dumps({"newmodel": {"input": 5.0, "output": 10.0,
                                                 "cache_write": 0.0, "cache_read": 0.0}}))
    lines = [_line("claude-newmodel-1", input_tokens=1_000_000, output_tokens=0)]
    result = cost.summarize_usage(lines)
    assert result["cost_usd"] == 5.0


def test_price_override_env_malformed_json_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("VOXA_PRICE_OVERRIDE_JSON", "{not json")
    lines = [_line("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)]
    result = cost.summarize_usage(lines)
    assert result["cost_usd"] == round(cost.PRICES["sonnet"]["input"], 4)


def test_no_override_env_uses_built_in_prices(monkeypatch):
    monkeypatch.delenv("VOXA_PRICE_OVERRIDE_JSON", raising=False)
    lines = [_line("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)]
    result = cost.summarize_usage(lines)
    assert result["cost_usd"] == round(cost.PRICES["sonnet"]["input"], 4)


# --- session_cost ------------------------------------------------------------

def _write_transcript(tmp_path, cwd, session_name, objs):
    from server.transcripts import _encode
    d = tmp_path / _encode(cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_name}.jsonl"
    with open(path, "w") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")
    return path


def test_session_cost_reads_transcript_and_summarizes(tmp_path):
    cwd = "/Users/x/project"
    objs = [
        _line("claude-sonnet-5", input_tokens=100, output_tokens=50),
        _line("claude-sonnet-5", input_tokens=200, output_tokens=100),
    ]
    _write_transcript(tmp_path, cwd, "sess1", objs)
    result = cost.session_cost(cwd, projects_dir=str(tmp_path))
    assert "error" not in result
    assert result["input_tokens"] == 300
    assert result["output_tokens"] == 150
    assert result["messages"] == 2


def test_session_cost_picks_the_newest_transcript(tmp_path):
    import time
    cwd = "/Users/x/project"
    p1 = _write_transcript(tmp_path, cwd, "older",
                            [_line("claude-sonnet-5", input_tokens=10, output_tokens=0)])
    time.sleep(0.01)
    p2 = _write_transcript(tmp_path, cwd, "newer",
                            [_line("claude-sonnet-5", input_tokens=999, output_tokens=0)])
    os.utime(p1, (1, 1))
    os.utime(p2, (2, 2))
    result = cost.session_cost(cwd, projects_dir=str(tmp_path))
    assert result["input_tokens"] == 999


def test_session_cost_missing_transcript_dir_returns_error(tmp_path):
    result = cost.session_cost("/nowhere/at/all", projects_dir=str(tmp_path))
    assert result == {"error": "no session transcript found"}


def test_session_cost_empty_cwd_returns_error(tmp_path):
    result = cost.session_cost("", projects_dir=str(tmp_path))
    assert result == {"error": "no session transcript found"}


def test_session_cost_tolerates_bad_lines(tmp_path):
    cwd = "/Users/x/project"
    from server.transcripts import _encode
    d = tmp_path / _encode(cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "sess.jsonl"
    with open(path, "w") as f:
        f.write("not json at all\n")
        f.write(json.dumps(_line("claude-sonnet-5", input_tokens=42, output_tokens=1)) + "\n")
        f.write("\n")  # blank line
    result = cost.session_cost(cwd, projects_dir=str(tmp_path))
    assert result["input_tokens"] == 42
    assert result["messages"] == 1


def test_session_cost_uses_default_projects_dir_when_unset(monkeypatch, tmp_path):
    # No projects_dir passed -> falls back to server.transcripts.PROJECTS_DIR.
    import server.transcripts as transcripts
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", str(tmp_path))
    cwd = "/Users/x/project"
    _write_transcript(tmp_path, cwd, "sess",
                      [_line("claude-sonnet-5", input_tokens=7, output_tokens=3)])
    result = cost.session_cost(cwd)
    assert result["input_tokens"] == 7
