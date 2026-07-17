"""The default operator factory's billing identity in metered (proxy) mode.

Regression (2026-07-17): with no per-connection account and no VOXA_ACCOUNT,
the factory fell back to the AUTH TOKEN as the billing account. That minted a
parallel free-trial identity on the cloud that silently burned out, after
which every /live connect was refused ("no minutes at connect") -> no Gemini
audio -> the phone fell back to its local voice. It also leaked the secret
token into billing records. The fallback must be the stable per-machine
anonymous device account instead, and the paired phone's id must always win.
"""
from __future__ import annotations

import server.app as app_module
import server.machine_id as machine_id_module
from server.config import Config


def _cfg() -> Config:
    return Config(gemini_api_key="", gemini_live_model="m",
                  auth_token="sekret-token", host="127.0.0.1", port=8787)


async def _htc(name, args):
    return {}


def test_metered_fallback_is_machine_device_account_not_auth_token(monkeypatch):
    monkeypatch.setenv("VOXA_LIVE_PROXY", "wss://cloud.example/live")
    monkeypatch.delenv("VOXA_ACCOUNT", raising=False)
    monkeypatch.delenv("VOXA_PROXY_TOKEN", raising=False)
    monkeypatch.setattr(machine_id_module, "machine_id", lambda path=None: "abc123")
    op = app_module._default_operator_factory(_cfg(), _htc)
    assert "account=d-abc123" in op._url
    assert "sekret-token" not in op._url


def test_metered_account_prefers_the_connections_account(monkeypatch):
    monkeypatch.setenv("VOXA_LIVE_PROXY", "wss://cloud.example/live")
    monkeypatch.delenv("VOXA_ACCOUNT", raising=False)
    op = app_module._default_operator_factory(_cfg(), _htc, account="acct-9")
    assert "account=acct-9" in op._url


def test_metered_account_env_pin_beats_machine_fallback(monkeypatch):
    monkeypatch.setenv("VOXA_LIVE_PROXY", "wss://cloud.example/live")
    monkeypatch.setenv("VOXA_ACCOUNT", "pinned-acct")
    op = app_module._default_operator_factory(_cfg(), _htc)
    assert "account=pinned-acct" in op._url
