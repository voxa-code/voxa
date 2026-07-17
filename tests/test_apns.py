# tests/test_apns.py
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from server.apns import (build_apns_jwt, build_voip_payload, build_cancel_payload,
                         build_alert_payload)


# Generate a throwaway P-256 key at runtime (never a real APNs key, and nothing
# key-shaped is committed to the repo, so secret scanners stay clean).
def _test_p8() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


TEST_P8 = _test_p8()

def test_voip_payload_shape():
    p = build_voip_payload("abc", "Claude finished")
    assert p["call_id"] == "abc"
    assert p["summary"] == "Claude finished"
    assert p["aps"]["content-available"] == 1

def test_cancel_payload_shape():
    p = build_cancel_payload("call-7")
    assert p["call_id"] == "call-7"
    assert p["type"] == "cancel"
    assert p["aps"]["content-available"] == 1
    assert "summary" not in p


def test_jwt_roundtrips_es256():
    token = build_apns_jwt(TEST_P8, "KEY123", "TEAM45", issued_at=1000)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY123"
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "TEAM45"
    assert claims["iat"] == 1000


def test_voip_payload_includes_approval_only_when_present():
    assert "approval" not in build_voip_payload("c1", "s")
    a = {"approval_id": "abc"}
    assert build_voip_payload("c1", "s", approval=a)["approval"] == a


# --- environment fallback: dev (sandbox) tokens vs production config ------------

from server.apns import ApnsClient


class _Cfg:
    apns_key = TEST_P8
    apns_key_path = ""
    apns_key_id = "KEY123"
    apns_team_id = "TEAM45"
    apns_bundle_id = "space.voxa.app"
    apns_sandbox = False           # production first (the cloud's config)


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _make_client(responder):
    """Patch ApnsClient._post to a scripted responder recording (host, token)."""
    c = ApnsClient(_Cfg())
    calls = []

    async def post(client, host, device_token, payload, push_type="voip", topic_suffix=".voip"):
        calls.append(host)
        return responder(host)
    c._post = post
    return c, calls


async def test_send_voip_falls_back_to_sandbox_on_bad_device_token():
    # A dev build's SANDBOX token against a production-configured cloud: prod
    # answers 400 BadDeviceToken, the sandbox retry lands the ring.
    def responder(host):
        if host == ApnsClient.PROD_HOST:
            return _Resp(400, '{"reason":"BadDeviceToken"}')
        return _Resp(200)
    c, calls = _make_client(responder)
    assert await c.send_voip("tok1", "c1", "done") is True
    assert calls == [ApnsClient.PROD_HOST, ApnsClient.SANDBOX_HOST]
    # The accepting host is remembered: the next push skips the failing round-trip.
    assert await c.send_voip("tok1", "c2", "done again") is True
    assert calls[2:] == [ApnsClient.SANDBOX_HOST]


async def test_send_voip_no_fallback_on_other_errors():
    # 410 Gone (dead token) must be returned as-is, not masked by a retry.
    c, calls = _make_client(lambda host: _Resp(410, '{"reason":"Unregistered"}'))
    assert await c.send_voip("tok2", "c1", "done") == 410
    assert calls == [ApnsClient.PROD_HOST]


async def test_send_voip_reports_double_bad_device_token_as_dead():
    # BadDeviceToken from BOTH environments means the token can never work
    # (stale install or wrong bundle/team): report 410 so the caller prunes it
    # instead of warning on every single ring forever.
    c, calls = _make_client(lambda host: _Resp(400, '{"reason":"BadDeviceToken"}'))
    assert await c.send_voip("tok3", "c1", "done") == 410
    assert calls == [ApnsClient.PROD_HOST, ApnsClient.SANDBOX_HOST]


async def test_cancel_uses_remembered_host():
    def responder(host):
        if host == ApnsClient.PROD_HOST:
            return _Resp(400, '{"reason":"BadDeviceToken"}')
        return _Resp(200)
    c, calls = _make_client(responder)
    await c.send_voip("tok4", "c1", "done")       # learns sandbox
    assert await c.send_voip_cancel("tok4", "c1") is True
    assert calls[-1] == ApnsClient.SANDBOX_HOST   # cancel went straight there


# --- send_alert (free-tier fallback push) ----------------------------------

def test_alert_payload_shape():
    p = build_alert_payload("Claude finished", "hi (Voxa Pro would have called you)")
    assert p["aps"]["alert"]["title"] == "Claude finished"
    assert p["aps"]["alert"]["body"] == "hi (Voxa Pro would have called you)"
    assert p["aps"]["sound"] == "default"
    assert "content-available" not in p["aps"]


async def test_send_alert_falls_back_to_sandbox_on_bad_device_token():
    def responder(host):
        if host == ApnsClient.PROD_HOST:
            return _Resp(400, '{"reason":"BadDeviceToken"}')
        return _Resp(200)
    c, calls = _make_client(responder)
    assert await c.send_alert("tok5", "Claude finished", "body") is True
    assert calls == [ApnsClient.PROD_HOST, ApnsClient.SANDBOX_HOST]


async def test_send_alert_returns_status_on_other_errors():
    c, calls = _make_client(lambda host: _Resp(410, '{"reason":"Unregistered"}'))
    assert await c.send_alert("tok6", "Claude finished", "body") == 410
    assert calls == [ApnsClient.PROD_HOST]


async def test_send_alert_uses_alert_push_type_and_bare_topic():
    """The real (unpatched) _post must build alert-specific headers: push-type
    "alert" and a topic WITHOUT the ".voip" suffix, unlike send_voip."""
    c = ApnsClient(_Cfg())
    seen = {}

    class _FakeResp:
        status_code = 200
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, content=None):
            seen["headers"] = headers
            return _FakeResp()

    import server.apns as apns_module
    orig = apns_module.httpx.AsyncClient
    apns_module.httpx.AsyncClient = _FakeAsyncClient
    try:
        assert await c.send_alert("tok7", "Claude finished", "body") is True
    finally:
        apns_module.httpx.AsyncClient = orig

    assert seen["headers"]["apns-push-type"] == "alert"
    assert seen["headers"]["apns-topic"] == _Cfg.apns_bundle_id  # no ".voip" suffix
    assert seen["headers"]["apns-priority"] == "10"


# --- persistent HTTP/2 connection reuse (A1) --------------------------------

class _FakeAsyncClientRecorder:
    """Records every construction so tests can assert the client is reused,
    not rebuilt per push. Mirrors the shape httpx.AsyncClient needs (async
    context manager methods present but unused by the persistent-client path)."""
    instances = 0

    def __init__(self, *a, **k):
        type(self).instances += 1
        self.is_closed = False
        self.init_kwargs = k

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        self.is_closed = True

    async def post(self, url, headers=None, content=None):
        class _R:
            status_code = 200
            text = ""
        return _R()


async def test_send_voip_reuses_the_same_client_across_calls():
    import server.apns as apns_module
    orig = apns_module.httpx.AsyncClient
    _FakeAsyncClientRecorder.instances = 0
    apns_module.httpx.AsyncClient = _FakeAsyncClientRecorder
    try:
        c = ApnsClient(_Cfg())
        await c.send_voip("tokA", "c1", "done")
        await c.send_voip("tokA", "c2", "done again")
        # Two pushes must not open two separate httpx.AsyncClient connections.
        assert _FakeAsyncClientRecorder.instances <= 1
    finally:
        apns_module.httpx.AsyncClient = orig


async def test_post_sends_apns_expiration_zero():
    """apns-expiration: 0 means a phone that comes online late never rings for
    a call that already timed out / was superseded."""
    c = ApnsClient(_Cfg())
    seen = {}

    class _FakeResp:
        status_code = 200
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def post(self, url, headers=None, content=None):
            seen["headers"] = headers
            return _FakeResp()

    await c._post(_FakeAsyncClient(), ApnsClient.PROD_HOST, "tokB", {"a": 1})
    assert seen["headers"]["apns-expiration"] == "0"


async def test_aclose_closes_the_shared_client():
    import server.apns as apns_module
    orig = apns_module.httpx.AsyncClient
    apns_module.httpx.AsyncClient = _FakeAsyncClientRecorder
    try:
        c = ApnsClient(_Cfg())
        await c.send_voip("tokC", "c1", "done")
        client = c._http()
        assert client.is_closed is False
        await c.aclose()
        assert client.is_closed is True
    finally:
        apns_module.httpx.AsyncClient = orig


# --- dead pooled connection recovery -----------------------------------------
# Regression: Apple closed the idle shared HTTP/2 connection (CLOSE-WAIT) and
# every push awaited it forever; /notify hung >90s with nothing logged and no
# ring ever went out (production, 2026-07-17). A push attempt must be bounded
# and retried once on a fresh client.


class _HangThenOkClient:
    """First instance's post() hangs forever; later instances answer 200.
    Mimics the dead pooled APNs connection + the healthy fresh one."""
    instances = 0

    def __init__(self, *a, **k):
        type(self).instances += 1
        self._hang = type(self).instances == 1
        self.is_closed = False

    async def aclose(self):
        self.is_closed = True

    async def post(self, url, headers=None, content=None):
        if self._hang:
            import asyncio
            await asyncio.Event().wait()   # never set: hangs like CLOSE-WAIT
        class _R:
            status_code = 200
            text = ""
        return _R()


async def test_send_voip_recovers_from_a_hung_pooled_connection():
    import server.apns as apns_module
    orig = apns_module.httpx.AsyncClient
    _HangThenOkClient.instances = 0
    apns_module.httpx.AsyncClient = _HangThenOkClient
    try:
        c = ApnsClient(_Cfg())
        c._attempt_timeout = 0.05      # don't make the test wait 12 real seconds
        assert await c.send_voip("tokH", "c1", "done") is True
        # The hung client was dropped and a fresh one made the successful push.
        assert _HangThenOkClient.instances == 2
    finally:
        apns_module.httpx.AsyncClient = orig


async def test_send_voip_recovers_from_a_transport_error():
    import httpx as _httpx
    import server.apns as apns_module

    class _RaiseThenOkClient(_HangThenOkClient):
        async def post(self, url, headers=None, content=None):
            if self._hang:
                raise _httpx.ConnectError("connection reset")
            return await super().post(url, headers=headers, content=content)

    orig = apns_module.httpx.AsyncClient
    _RaiseThenOkClient.instances = 0
    apns_module.httpx.AsyncClient = _RaiseThenOkClient
    try:
        c = ApnsClient(_Cfg())
        assert await c.send_voip("tokE", "c1", "done") is True
        assert _RaiseThenOkClient.instances == 2
    finally:
        apns_module.httpx.AsyncClient = orig


async def test_shared_client_sets_a_keepalive_expiry():
    """An idle pooled connection must expire client-side before Apple's idle
    disconnect can leave a dead-but-pooled connection behind."""
    import server.apns as apns_module
    orig = apns_module.httpx.AsyncClient
    apns_module.httpx.AsyncClient = _FakeAsyncClientRecorder
    try:
        c = ApnsClient(_Cfg())
        limits = c._http().init_kwargs.get("limits")
        assert limits is not None and limits.keepalive_expiry == 60
    finally:
        apns_module.httpx.AsyncClient = orig


async def test_send_voip_still_uses_voip_topic_after_refactor():
    """Guard against the shared _send()/_post() refactor accidentally dropping
    the ".voip" topic suffix for the original voip push path."""
    c = ApnsClient(_Cfg())
    seen = {}

    class _FakeResp:
        status_code = 200
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, content=None):
            seen["headers"] = headers
            return _FakeResp()

    import server.apns as apns_module
    orig = apns_module.httpx.AsyncClient
    apns_module.httpx.AsyncClient = _FakeAsyncClient
    try:
        assert await c.send_voip("tok8", "c1", "done") is True
    finally:
        apns_module.httpx.AsyncClient = orig

    assert seen["headers"]["apns-push-type"] == "voip"
    assert seen["headers"]["apns-topic"] == f"{_Cfg.apns_bundle_id}.voip"
