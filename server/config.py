from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Config:
    gemini_api_key: str
    gemini_live_model: str
    auth_token: str
    host: str
    port: int
    apns_key_path: str = ""
    apns_key: str = ""          # the .p8 contents (preferred on container hosts)
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_bundle_id: str = ""
    apns_sandbox: bool = False

    @property
    def push_enabled(self) -> bool:
        return all([
            (self.apns_key or self.apns_key_path), self.apns_key_id,
            self.apns_team_id, self.apns_bundle_id,
        ])


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    api_key = env.get("GEMINI_API_KEY", "").strip()
    auth_token = env.get("VOXA_AUTH_TOKEN", "").strip()
    # GEMINI_API_KEY is required only in DIRECT mode (Gemini runs locally). In the
    # metered/relay product the laptop has no key, the cloud /live proxy holds it,
    # so it's optional here and only the server enforces having one.
    if not api_key and not env.get("VOXA_LIVE_PROXY", "").strip():
        raise ValueError("GEMINI_API_KEY is required (or set VOXA_LIVE_PROXY for metered mode)")
    if not auth_token:
        raise ValueError("VOXA_AUTH_TOKEN is required")
    apns = {
        "apns_key_path": env.get("APNS_KEY_PATH", "").strip(),
        "apns_key": env.get("APNS_KEY", ""),
        "apns_key_id": env.get("APNS_KEY_ID", "").strip(),
        "apns_team_id": env.get("APNS_TEAM_ID", "").strip(),
        "apns_bundle_id": env.get("APNS_BUNDLE_ID", "").strip(),
        "apns_sandbox": env.get("APNS_SANDBOX", "").strip().lower() in ("1", "true", "yes"),
    }
    return Config(
        gemini_api_key=api_key,
        gemini_live_model=env.get("GEMINI_LIVE_MODEL", "gemini-2.0-flash-live-001").strip(),
        auth_token=auth_token,
        host=env.get("VOXA_HOST", "127.0.0.1").strip(),
        port=int(env.get("VOXA_PORT", "8787")),
        **apns,
    )
