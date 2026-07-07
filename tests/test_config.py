import pytest
from server.config import load_config, Config


def test_load_config_reads_values():
    cfg = load_config({
        "GEMINI_API_KEY": "k",
        "VOXA_AUTH_TOKEN": "t",
        "GEMINI_LIVE_MODEL": "model-x",
        "VOXA_HOST": "0.0.0.0",
        "VOXA_PORT": "9000",
    })
    assert cfg == Config("k", "model-x", "t", "0.0.0.0", 9000)


def test_load_config_defaults():
    cfg = load_config({"GEMINI_API_KEY": "k", "VOXA_AUTH_TOKEN": "t"})
    assert cfg.gemini_live_model == "gemini-2.0-flash-live-001"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8787


@pytest.mark.parametrize("env", [
    {"VOXA_AUTH_TOKEN": "t"},          # missing key
    {"GEMINI_API_KEY": "k"},           # missing token
    {"GEMINI_API_KEY": "", "VOXA_AUTH_TOKEN": "t"},  # empty key
])
def test_load_config_requires_secrets(env):
    with pytest.raises(ValueError):
        load_config(env)


def test_metered_mode_makes_gemini_key_optional():
    # The laptop client runs no Gemini key; the cloud /live proxy holds it. With
    # VOXA_LIVE_PROXY set, a missing GEMINI_API_KEY must NOT raise.
    cfg = load_config({"VOXA_AUTH_TOKEN": "t", "VOXA_LIVE_PROXY": "wss://x/live"})
    assert cfg.gemini_api_key == ""
    # Auth token is still required even in metered mode.
    with pytest.raises(ValueError):
        load_config({"VOXA_LIVE_PROXY": "wss://x/live"})


def test_push_disabled_when_apns_unset():
    cfg = load_config({"GEMINI_API_KEY": "k", "VOXA_AUTH_TOKEN": "t"})
    assert cfg.push_enabled is False
    assert cfg.apns_bundle_id == ""


def test_push_enabled_when_all_apns_set():
    cfg = load_config({
        "GEMINI_API_KEY": "k", "VOXA_AUTH_TOKEN": "t",
        "APNS_KEY_PATH": "/k.p8", "APNS_KEY_ID": "ABC123",
        "APNS_TEAM_ID": "TEAM12", "APNS_BUNDLE_ID": "com.x.voxa",
    })
    assert cfg.push_enabled is True
    assert cfg.apns_key_id == "ABC123"
