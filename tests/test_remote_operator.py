import json

from server.remote_operator import RemoteOperator


class FakeWS:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def send(self, m):
        self.sent.append(m)

    def __aiter__(self):
        self._it = iter(self.incoming)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


async def test_run_routes_audio_tools_and_transcripts():
    audio, texts, tools = [], [], []

    async def handle(name, args):
        tools.append((name, args))
        return {"ok": True}

    op = RemoteOperator(object(), handle, proxy_url="wss://x/live", account="a", token="t")
    op._ws = FakeWS([
        b"\x01\x02",
        json.dumps({"type": "tool", "id": "1", "name": "send_to_claude", "args": {"text": "hi"}}),
        json.dumps({"type": "transcript", "role": "agent", "text": "done"}),
    ])

    async def aout(b): audio.append(b)
    async def tout(d): texts.append(d)
    op.set_audio_out(aout)
    op.set_text_out(tout)

    await op.run()

    assert audio == [b"\x01\x02"]
    assert ("send_to_claude", {"text": "hi"}) in tools
    assert any("tool_result" in s for s in op._ws.sent)          # replied to the cloud
    assert {"type": "transcript", "role": "agent", "text": "done"} in texts


async def test_send_audio_and_speak():
    op = RemoteOperator(object(), None, proxy_url="wss://x/live", account="a")
    op._ws = FakeWS([])
    await op.send_audio(b"\x09")
    await op.speak("hello there")
    assert b"\x09" in op._ws.sent
    assert any('"speak"' in s and "hello there" in s
               for s in op._ws.sent if isinstance(s, str))


def test_url_includes_account_and_token():
    op = RemoteOperator(object(), None, proxy_url="wss://api.voxa.space/live",
                        account="acct1", token="tok", voice="Puck")
    assert "account=acct1" in op._url and "token=tok" in op._url and "voice=Puck" in op._url


async def test_send_text_forwards_user_text():
    # A typed turn during a metered call must forward a user_text frame, not raise
    # AttributeError (which previously tore the whole call down).
    op = RemoteOperator(object(), None, proxy_url="wss://x/live", account="a")
    op._ws = FakeWS([])
    await op.send_text("add tests please")
    assert any('"user_text"' in s and "add tests please" in s
               for s in op._ws.sent if isinstance(s, str))


async def test_suppress_greeting_is_sent_before_next_speak():
    # suppress_greeting() defers a frame that must be sent in-order, immediately before
    # the next speak(), on this single socket writer (no concurrent send).
    op = RemoteOperator(object(), None, proxy_url="wss://x/live", account="a")
    op._ws = FakeWS([])
    op.suppress_greeting()
    await op.speak("you're back in loop", immediate=True)
    kinds = [json.loads(s)["type"] for s in op._ws.sent if isinstance(s, str)]
    assert kinds == ["suppress_greeting", "speak"]
    assert json.loads(op._ws.sent[-1])["immediate"] is True


async def test_send_text_does_not_crash_when_cloud_closed():
    op = RemoteOperator(object(), lambda n, a: None, proxy_url="ws://x", account="a")

    class BadWS:
        async def send(self, m):
            raise RuntimeError("connection closed")

    op._ws = BadWS()
    await op.send_text("hello")   # must not raise


async def test_sends_do_not_crash_when_cloud_closed():
    # When the cloud /live link is closed (e.g. out of minutes -> 4402), speak and
    # send_audio must NOT raise (that crashed the answer flow). They no-op instead.
    op = RemoteOperator(object(), lambda n, a: None, proxy_url="ws://x", account="a")
    class BadWS:
        async def send(self, m):
            raise RuntimeError("connection closed")
    op._ws = BadWS()
    await op.speak("you're back in loop")   # must not raise
    await op.send_audio(b"\x00\x01")        # must not raise


def test_lang_rides_the_live_proxy_url():
    op = RemoteOperator(object(), None, proxy_url="wss://cloud/live",
                        account="acct1", token="tok", voice="Puck", lang="ar")
    assert "lang=ar" in op._url


async def test_speak_accepts_dedupe_key_for_signature_parity():
    # The multi-session foreign-update path calls speak(..., dedupe_key=cwd) on
    # WHATEVER operator is live; on the metered path that is RemoteOperator, and
    # a missing parameter raised TypeError and silenced the update entirely.
    op = RemoteOperator(object(), None, proxy_url="wss://x/live", account="a")
    op._ws = FakeWS([])
    await op.speak("veil finished: tests pass", immediate=True, dedupe_key="/tmp/veil")
    assert any('"speak"' in s and "veil finished" in s
               for s in op._ws.sent if isinstance(s, str))


async def test_open_with_context_forwards_one_frame_with_tail_capped_context():
    op = RemoteOperator(object(), None, proxy_url="wss://x/live", account="a")
    op._ws = FakeWS([])
    ctx = "OLD-" + "x" * 7000 + "-NEWEST"
    await op.open_with_context("Hi, you're back in loop.", ctx)
    frames = [json.loads(s) for s in op._ws.sent if isinstance(s, str)]
    ows = [f for f in frames if f.get("type") == "open_with_context"]
    assert len(ows) == 1
    assert ows[0]["opening"] == "Hi, you're back in loop."
    assert ows[0]["context"].endswith("-NEWEST")     # tail survives the cap
    assert "OLD-" not in ows[0]["context"]           # head is what gets cut
    assert len(ows[0]["context"]) <= 6000


async def test_open_with_context_flushes_pending_greeting_suppression_first():
    op = RemoteOperator(object(), None, proxy_url="wss://x/live", account="a")
    op._ws = FakeWS([])
    op.suppress_greeting()
    await op.open_with_context("Hello.", "some context")
    types_sent = [json.loads(s).get("type") for s in op._ws.sent if isinstance(s, str)]
    assert types_sent.index("suppress_greeting") < types_sent.index("open_with_context")
