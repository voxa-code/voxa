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


def build_alert_payload(title: str, body: str) -> dict:
    """Plain visual alert (not a VoIP ring): used for the free-tier fallback
    push, "Claude finished... Voxa Pro would have called you"."""
    return {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}


class ApnsClient:
    """Sends VoIP and plain-alert pushes via APNs HTTP/2. One per server process."""

    PROD_HOST = "https://api.push.apple.com"
    SANDBOX_HOST = "https://api.sandbox.push.apple.com"

    def __init__(self, config, now_fn=None):
        self._cfg = config
        # Xcode/dev-signed builds get sandbox push tokens, which only work
        # against the sandbox host; VOXA_SANDBOX (via config.apns_sandbox) picks
        # the host tried FIRST; a BadDeviceToken answer makes _send retry the
        # other one (one registry serves both build types).
        self._host = self.SANDBOX_HOST if getattr(config, "apns_sandbox", False) else self.PROD_HOST
        # device token -> the host that last ACCEPTED it, so after one fallback
        # round-trip every later push (and cancel) goes straight to the right
        # environment. In-memory: worst case after a restart is one extra retry.
        self._token_host: dict[str, str] = {}
        import time
        self._now = now_fn or (lambda: int(time.time()))
        self._jwt = ""
        self._jwt_at = 0
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        """One shared HTTP/2 connection to APNs for the process lifetime.
        Apple throttles rapid connect/disconnect, and a cold TLS handshake
        adds seconds to trigger-to-ring; reuse is the fix."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(http2=True, timeout=10)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

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

    def _other_host(self, host: str) -> str:
        return self.PROD_HOST if host == self.SANDBOX_HOST else self.SANDBOX_HOST

    async def _post(self, client, host: str, device_token: str, payload: dict,
                     push_type: str = "voip", topic_suffix: str = ".voip"):
        return await client.post(
            f"{host}/3/device/{device_token}",
            headers={
                "apns-topic": f"{self._cfg.apns_bundle_id}{topic_suffix}",
                "apns-push-type": push_type,
                "apns-priority": "10",
                "authorization": f"bearer {self._token()}",
                # 0 = the notification never persists at Apple past this attempt.
                # A phone that comes back online late must not get a ring for a
                # call that already timed out or was superseded.
                "apns-expiration": "0",
            },
            content=json.dumps(payload),
        )

    async def _send(self, device_token: str, payload: dict, push_type: str,
                     topic_suffix: str) -> bool | int:
        """Shared BadDeviceToken environment-fallback used by both send_voip and
        send_alert: try the token's remembered (or configured default) host
        first. A token minted by the OTHER environment (a dev build's sandbox
        token when we default to production, or vice versa) answers 400
        BadDeviceToken; one device registry serves both build types, so retry
        the other host and remember which one accepted this token.

        Returns True on success, or the HTTP status code on failure (so the
        caller can prune a 410 Gone / dead token)."""
        host = self._token_host.get(device_token, self._host)
        client = self._http()
        resp = await self._post(client, host, device_token, payload, push_type, topic_suffix)
        if resp.status_code == 200:
            self._token_host[device_token] = host
            return True
        if resp.status_code == 400 and "BadDeviceToken" in resp.text:
            other = self._other_host(host)
            retry = await self._post(client, other, device_token, payload, push_type, topic_suffix)
            if retry.status_code == 200:
                self._token_host[device_token] = other
                logger.info("APNs %s push ok on %s after BadDeviceToken on %s "
                            "(token=%s)", push_type, other, host, device_token[:8])
                return True
            resp = retry
            if resp.status_code == 400 and "BadDeviceToken" in resp.text:
                # BOTH environments rejected this token, so it can never work
                # (a stale token from an old install/build, or wrong bundle/
                # team). Report it as 410 so the caller prunes it instead of
                # warning about it on every single ring.
                logger.warning("APNs %s push: BadDeviceToken on both hosts; "
                               "reporting token %s as dead for pruning",
                               push_type, device_token[:8])
                return 410
        # 410 = the token is dead (app deleted/reinstalled); other codes are
        # transient/config. Log the reason so silent no-rings are diagnosable.
        logger.warning("APNs %s push failed: status=%s body=%s token=%s",
                       push_type, resp.status_code, resp.text[:200], device_token[:8])
        return resp.status_code

    async def send_voip(self, device_token: str, call_id: str, summary: str,
                         approval: dict | None = None) -> bool | int:
        """Send a VoIP ring. Returns True on success, or the HTTP status code on
        failure (so the caller can prune a 410 Gone / dead token)."""
        payload = build_voip_payload(call_id, summary, approval=approval)
        return await self._send(device_token, payload, "voip", ".voip")

    async def send_alert(self, device_token: str, title: str, body: str) -> bool | int:
        """Send a plain visual alert push (no CallKit ring): used for the
        free-tier fallback when an account is past its monthly call quota.
        apns-push-type is "alert" and apns-topic is the bare bundle id (no
        ".voip" suffix), per Apple's requirements for non-VoIP pushes. Same
        BadDeviceToken environment-fallback as send_voip."""
        payload = build_alert_payload(title, body)
        return await self._send(device_token, payload, "alert", "")

    async def send_voip_cancel(self, device_token: str, call_id: str) -> bool:
        payload = build_cancel_payload(call_id)
        host = self._token_host.get(device_token, self._host)
        client = self._http()
        resp = await self._post(client, host, device_token, payload)
        if resp.status_code == 200:
            return True
        if resp.status_code == 400 and "BadDeviceToken" in resp.text:
            retry = await self._post(client, self._other_host(host),
                                     device_token, payload)
            return retry.status_code == 200
        return False
