"""Spoken cost/token meter for the attached Claude Code session.

Reads Claude Code's own transcript JSONL (the same file server.transcripts
resolves for a cwd: ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``) and
sums every assistant message's token usage into a readable total plus an
estimated USD cost, so Voxa can answer "how much has this session cost so
far?" or "how many tokens?" from the real, local session data. No network
calls, no external services: everything here is pure and file-local.

PRICING NOTE: PRICES below are approximate published-rate estimates (USD per
1,000,000 tokens) for the current Claude model families as of 2026, matched
by SUBSTRING against the model id reported in the transcript (e.g.
"claude-fable-5" matches the "fable" tier, "claude-haiku-4-5-20251001"
matches "haiku"), so a new dated snapshot of an existing family still prices
correctly without a code change. Rates drift and Anthropic may reprice at
any time: this module never hardcode-locks them. Set the environment
variable VOXA_PRICE_OVERRIDE_JSON to a JSON object shaped like PRICES (whole
tiers or individual keys) to override or add tiers, e.g.
'{"sonnet": {"input": 3.5}}'.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

# USD per 1,000,000 tokens. Approximate, overridable via VOXA_PRICE_OVERRIDE_JSON.
PRICES: dict[str, dict[str, float]] = {
    "fable": {
        "input": 22.50, "output": 112.50,
        "cache_write": 28.125, "cache_read": 2.25,
    },
    "opus": {
        "input": 15.00, "output": 75.00,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "sonnet": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "haiku": {
        "input": 0.80, "output": 4.00,
        "cache_write": 1.00, "cache_read": 0.08,
    },
    # Fallback tier for an unrecognised model id, priced like Sonnet (the
    # default workhorse tier) so an unknown/future model never prices as free
    # or wildly wrong.
    "default": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    },
}

# Substring match order: more specific/newer family names first so e.g. a
# hypothetical "claude-fable-haiku" (unlikely, but substrings are cheap to
# get wrong) prefers the leading tier name. Each entry must be a key in PRICES.
_TIER_ORDER = ("fable", "opus", "sonnet", "haiku")


def _prices() -> dict[str, dict[str, float]]:
    """PRICES merged with any VOXA_PRICE_OVERRIDE_JSON override. Re-read on
    every call (the dict is tiny) so tests can set/unset the env var freely
    without needing to reload this module."""
    prices = {tier: dict(rates) for tier, rates in PRICES.items()}
    raw = os.environ.get("VOXA_PRICE_OVERRIDE_JSON", "").strip()
    if not raw:
        return prices
    try:
        override = json.loads(raw)
    except (ValueError, TypeError):
        return prices
    if not isinstance(override, dict):
        return prices
    for tier, rates in override.items():
        if isinstance(rates, dict):
            prices.setdefault(tier, {}).update(rates)
    return prices


def _tier_for(model: str, prices: dict) -> str:
    """Match ``model`` to a pricing tier by substring. Checks the built-in
    families first (in ``_TIER_ORDER``) then any tier ADDED by
    VOXA_PRICE_OVERRIDE_JSON, so an override can introduce a brand-new model
    family (not just retune an existing tier's numbers) and still have it
    matched by substring instead of silently falling back to "default"."""
    m = (model or "").lower()
    for tier in _TIER_ORDER:
        if tier in m:
            return tier
    for tier in prices:
        if tier != "default" and tier not in _TIER_ORDER and tier in m:
            return tier
    return "default"


def _rate(prices: dict, model: str, key: str) -> float:
    tier = _tier_for(model, prices)
    rates = prices.get(tier) or prices["default"]
    return float(rates.get(key, 0.0))


def _message_cost(prices: dict, model: str, usage: dict) -> float:
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    total = (
        inp * _rate(prices, model, "input")
        + out * _rate(prices, model, "output")
        + cache_write * _rate(prices, model, "cache_write")
        + cache_read * _rate(prices, model, "cache_read")
    )
    return total / 1_000_000.0


def _empty_model_entry() -> dict:
    return {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "total_tokens": 0, "cost_usd": 0.0, "messages": 0,
    }


def summarize_usage(lines: Iterable[dict]) -> dict:
    """Sum token usage and cost across parsed transcript objects (already
    decoded JSON dicts, one per JSONL line, as Claude Code writes them).

    Only assistant messages carrying a ``message.usage`` dict contribute;
    anything else (user turns, tool results, a missing or malformed usage
    block, a non-dict line) is skipped, never raises. Cost is priced PER
    MESSAGE using that message's own ``message.model``, so a session that
    mixes models (e.g. Sonnet for most turns, Opus for one) is still priced
    correctly rather than averaged into one rate.
    """
    prices = _prices()
    input_tokens = output_tokens = cache_read = cache_write = 0
    cost = 0.0
    by_model: dict[str, dict] = {}
    messages = 0

    for obj in lines:
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        model = message.get("model") or "unknown"
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        cw = int(usage.get("cache_creation_input_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        msg_cost = _message_cost(prices, model, usage)

        input_tokens += inp
        output_tokens += out
        cache_write += cw
        cache_read += cr
        cost += msg_cost
        messages += 1

        entry = by_model.setdefault(model, _empty_model_entry())
        entry["input_tokens"] += inp
        entry["output_tokens"] += out
        entry["cache_read_tokens"] += cr
        entry["cache_write_tokens"] += cw
        entry["total_tokens"] += inp + out + cr + cw
        entry["cost_usd"] += msg_cost
        entry["messages"] += 1

    for entry in by_model.values():
        entry["cost_usd"] = round(entry["cost_usd"], 4)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": input_tokens + output_tokens + cache_read + cache_write,
        "cost_usd": round(cost, 4),
        "by_model": by_model,
        "messages": messages,
    }


def _iter_transcript_lines(path: str):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def session_cost(cwd: str, projects_dir: str | None = None) -> dict:
    """Locate the newest Claude Code transcript for ``cwd`` and summarize its
    token usage and estimated cost. Reuses server.transcripts' own cwd -> file
    resolution (encoding + newest-mtime pick), so this always looks at the
    exact same transcript the recap/read_session tools do.

    Fail-open: no cwd, no transcript directory, an empty transcript, or any
    read error returns ``{"error": "no session transcript found"}`` instead
    of raising, since this backs a live voice tool that must never crash a
    call.
    """
    if not cwd:
        return {"error": "no session transcript found"}
    from server.transcripts import PROJECTS_DIR, latest_transcript

    path = latest_transcript(cwd, projects_dir or PROJECTS_DIR)
    if not path:
        return {"error": "no session transcript found"}
    try:
        return summarize_usage(_iter_transcript_lines(path))
    except OSError:
        return {"error": "no session transcript found"}
