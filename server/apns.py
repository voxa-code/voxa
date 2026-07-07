from __future__ import annotations

import json
import logging

import httpx
import jwt

logger = logging.getLogger(__name__)


def build_apns_jwt(key_pem: str, key_id: str, team_id: str, issued_at: int) -> str:
    return jwt.encode(
        {"iss": team_id, "iat": issued_at},
        key_pem,
        algorithm="ES256",
        headers={"alg": "ES256", "kid": key_id},
    )


def build_voip_payload(call_id: str, summary: str, approval: dict | None = None) -> dict:
    payload = {"call_id": call_id, "summary": summary, "aps": {"content-available": 1}}
    if approval is not None:
        payload["approval"] = approval
    return payload


def build_cancel_payload(call_id: str) -> dict:
    return {"call_id": call_id, "type": "cancel", "aps": {"content-available": 1}}


class ApnsClient:
    """Sends VoIP pushes via APNs HTTP/2. One per server process."""

    PROD_HOST = "https://api.push.apple.com"
    SANDBOX_HOST = "https://api.sandbox.push.apple.com"

    def __init__(self, config, now_fn=None):
        self._cfg = config
        # Xcode/dev-signed builds get sandbox push tokens, which only work
        # against the sandbox host; TestFlight/App Store builds use production.
        self._host = self.SANDBOX_HOST if getattr(config, "apns_sandbox", False) else self.PROD_HOST
        import time
        self._now = now_fn or (lambda: int(time.time()))
        self._jwt = ""
        self._jwt_at = 0

    def _token(self) -> str:
        now = self._now()
        if not self._jwt or now - self._jwt_at > 50 * 60:
            # Prefer the key contents (set as a secret on container hosts); fall
            # back to a file path for local/dev use.
            key_pem = getattr(self._cfg, "apns_key", "") or open(self._cfg.apns_key_path).read()
            self._jwt = build_apns_jwt(
                key_pem, self._cfg.apns_key_id, self._cfg.apns_team_id, now
            )
            self._jwt_at = now
        return self._jwt

    async def send_voip(self, device_token: str, call_id: str, summary: str,
                         approval: dict | None = None) -> bool | int:
        """Send a VoIP ring. Returns True on success, or the HTTP status code on
        failure (so the caller can prune a 410 Gone / dead token)."""
        url = f"{self._host}/3/device/{device_token}"
        headers = {
            "apns-topic": f"{self._cfg.apns_bundle_id}.voip",
            "apns-push-type": "voip",
            "apns-priority": "10",
            "authorization": f"bearer {self._token()}",
        }
        payload = build_voip_payload(call_id, summary, approval=approval)
        async with httpx.AsyncClient(http2=True, timeout=10) as client:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
            if resp.status_code != 200:
                # 410 = the token is dead (app deleted/reinstalled); other codes are
                # transient/config. Log the reason so silent no-rings are diagnosable.
                logger.warning("APNs voip push failed: status=%s body=%s token=%s",
                               resp.status_code, resp.text[:200], device_token[:8])
                return resp.status_code
            return True

    async def send_voip_cancel(self, device_token: str, call_id: str) -> bool:
        url = f"{self._host}/3/device/{device_token}"
        headers = {
            "apns-topic": f"{self._cfg.apns_bundle_id}.voip",
            "apns-push-type": "voip",
            "apns-priority": "10",
            "authorization": f"bearer {self._token()}",
        }
        payload = build_cancel_payload(call_id)
        async with httpx.AsyncClient(http2=True, timeout=10) as client:
            resp = await client.post(url, headers=headers, content=json.dumps(payload))
            return resp.status_code == 200
