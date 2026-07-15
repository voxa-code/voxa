# tests/test_call_manager.py
import asyncio
import time

import pytest
from server.call_manager import CallManager

class FakePusher:
    def __init__(self):
        self.sent = []
        self.cancelled = []
    async def send_voip(self, token, call_id, summary, approval=None):
        self.sent.append((token, call_id, summary)); return True
    async def send_voip_cancel(self, token, call_id):
        self.cancelled.append((token, call_id)); return True

class FakeRegistry:
    def tokens(self, account=None): return ["DEV1"]

@pytest.mark.asyncio
async def test_no_ring_when_line_open():
    cm = CallManager(FakePusher(), FakeRegistry())
    cm.attach()
    await cm.on_update("done")
    assert cm._pusher.sent == []

@pytest.mark.asyncio
async def test_ring_and_queue_when_closed():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    await cm.on_update("step 1 done")
    assert len(push.sent) == 1
    assert push.sent[0][0] == "DEV1"
    # answering drains the queue
    pending = cm.attach()
    assert pending == ["step 1 done"]
    assert cm.attach() == []  # drained

@pytest.mark.asyncio
async def test_decline_then_next_update_rings_again():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    await cm.on_update("u1")
    call1 = push.sent[-1][1]
    await cm.decline(call1)
    # same update should not double-ring; simulate a re-trigger of same update
    await cm.on_update("u2")   # a NEW update -> rings again
    assert len(push.sent) == 2
    assert push.sent[1][1] != call1


@pytest.mark.asyncio
async def test_cancel_sends_cancel_push_for_last_call():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    await cm.ring("acct", "finished")
    last = push.sent[-1][1]
    await cm.cancel("acct")
    assert push.cancelled == [("DEV1", last)]


@pytest.mark.asyncio
async def test_cancel_noop_when_nothing_rung():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    await cm.cancel("acct")
    assert push.cancelled == []


@pytest.mark.asyncio
async def test_queue_does_not_ring():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    cm.queue("app open, spoken on start")
    assert push.sent == []                       # queued only, no ring
    assert cm.attach() == ["app open, spoken on start"]


@pytest.mark.asyncio
async def test_pending_queue_is_bounded():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    for i in range(50):
        cm.queue(f"u{i}")
    assert len(cm._pending) <= 10
    assert cm._pending[-1] == "u49"              # newest kept


@pytest.mark.asyncio
async def test_decline_dedupes_and_is_bounded():
    cm = CallManager(FakePusher(), FakeRegistry())
    await cm.decline("c1")
    await cm.decline("c1")                        # duplicate ignored
    assert cm._declined.count("c1") == 1
    for i in range(100):
        await cm.decline(f"c-{i}")
    assert len(cm._declined) <= 50


@pytest.mark.asyncio
async def test_decline_cancels_ring_on_account_devices():
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    await cm.ring("acct", "finished")
    call_id = push.sent[-1][1]
    await cm.decline(call_id)
    assert push.cancelled == [("DEV1", call_id)]


@pytest.mark.asyncio
async def test_ring_prunes_dead_410_token():
    class GonePusher:
        def __init__(self): self.cancelled = []
        async def send_voip(self, token, call_id, summary, approval=None): return 410
    class RecordingRegistry:
        def __init__(self): self.removed = []
        def tokens(self, account=None): return ["DEAD"]
        def remove(self, token): self.removed.append(token)
    reg = RecordingRegistry()
    cm = CallManager(GonePusher(), reg)
    await cm.ring("acct", "finished")
    assert reg.removed == ["DEAD"]


@pytest.mark.asyncio
async def test_answering_consumes_ring_so_cancel_is_silent():
    # The phone ANSWERED (line attached): a later cancel, e.g. the watcher seeing
    # the terminal "resume" because Voxa itself is driving it, must not push the
    # answering phone a spurious extra call.
    push = FakePusher()
    cm = CallManager(push, FakeRegistry())
    await cm.ring("acct", "finished")
    cm.attach()                                   # user answered
    await cm.cancel("acct")
    assert push.cancelled == []


@pytest.mark.asyncio
async def test_recently_open_reflects_attach_detach():
    cm = CallManager(FakePusher(), FakeRegistry())
    assert not cm.recently_open()                 # never attached
    cm.attach()
    assert cm.recently_open()                     # line is open
    cm.detach()
    assert cm.recently_open()                     # just closed -> still "recent"
    assert not cm.recently_open(within=0.0)       # zero window -> immediately stale


@pytest.mark.asyncio
async def test_ring_survives_push_failure():
    class FlakyPusher:
        def __init__(self): self.calls = 0
        async def send_voip(self, token, call_id, summary, approval=None):
            self.calls += 1
            raise RuntimeError("apns down")
    cm = CallManager(FlakyPusher(), FakeRegistry())
    await cm.on_update("done")                   # must not raise despite push failure
    assert cm._pusher.calls == 1


@pytest.mark.asyncio
async def test_ring_pushes_multiple_tokens_in_parallel_not_sequentially():
    # 3 registered tokens, each push sleeps 0.05s: if ring() awaited them one at
    # a time the whole call would take >= 0.15s. Overlapped, it stays well under
    # that (generous margin so this never flakes on a loaded CI box).
    class SlowPusher:
        def __init__(self):
            self.sent = []
        async def send_voip(self, token, call_id, summary, approval=None):
            self.sent.append(token)
            await asyncio.sleep(0.05)
            return True

    class ThreeTokenRegistry:
        def tokens(self, account=None):
            return ["DEV1", "DEV2", "DEV3"]

    push = SlowPusher()
    cm = CallManager(push, ThreeTokenRegistry())
    start = time.monotonic()
    await cm.ring("acct", "finished")
    elapsed = time.monotonic() - start
    assert elapsed < 0.12                       # overlapped, not 3 x 0.05s
    assert sorted(push.sent) == ["DEV1", "DEV2", "DEV3"]


@pytest.mark.asyncio
async def test_ring_one_raising_token_does_not_block_the_others():
    class FlakyThreeTokenPusher:
        def __init__(self):
            self.sent = []
        async def send_voip(self, token, call_id, summary, approval=None):
            if token == "BAD":
                raise RuntimeError("apns down for this token")
            self.sent.append(token)
            return True

    class ThreeTokenRegistry:
        def tokens(self, account=None):
            return ["DEV1", "BAD", "DEV2"]

    push = FlakyThreeTokenPusher()
    cm = CallManager(push, ThreeTokenRegistry())
    await cm.ring("acct", "finished")             # must not raise
    assert sorted(push.sent) == ["DEV1", "DEV2"]


@pytest.mark.asyncio
async def test_ring_prunes_dead_410_token_among_parallel_pushes():
    class MixedResultPusher:
        async def send_voip(self, token, call_id, summary, approval=None):
            return 410 if token == "DEAD" else True

    class RecordingRegistry:
        def __init__(self):
            self.removed = []
        def tokens(self, account=None):
            return ["DEV1", "DEAD", "DEV2"]
        def remove(self, token):
            self.removed.append(token)

    reg = RecordingRegistry()
    cm = CallManager(MixedResultPusher(), reg)
    await cm.ring("acct", "finished")
    assert reg.removed == ["DEAD"]


@pytest.mark.asyncio
async def test_queue_and_attach_approvals():
    cm = CallManager(FakePusher(), FakeRegistry())
    cm.queue("s1", approval={"approval_id": "a1"})
    cm.queue("s2")
    assert cm.attach() == ["s1", "s2"]            # summaries unchanged
    assert [a["approval_id"] for a in cm.attach_approvals()] == ["a1"]
    assert cm.attach_approvals() == []            # drained
