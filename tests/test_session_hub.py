import pytest
from server.session_hub import SessionHub

class FakeCM:
    def __init__(self): self.updates = []; self.attached = False
    async def on_update(self, s): self.updates.append(s)
    def attach(self): self.attached = True; return ["queued"]
    def detach(self): self.attached = False

class FakeController:
    def on_final(self, cb): self._cb = cb

@pytest.mark.asyncio
async def test_speaks_when_attached_else_queues():
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    spoken = []
    pending = hub.attach(lambda t: spoken.append(t))
    assert pending == ["queued"]
    await hub.on_final("hi")
    assert spoken == ["hi"] and cm.updates == []
    hub.detach()
    await hub.on_final("later")
    assert cm.updates == ["later"]


@pytest.mark.asyncio
async def test_offline_ring_can_be_disabled():
    # Once hooks are live, the hub must NOT ring on finish when detached (the hook
    # becomes the single offline-ring source).
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    hub.set_offline_ring(False)
    await hub.on_final("done")
    assert cm.updates == []      # suppressed; hook will ring instead
    hub.set_offline_ring(True)
    await hub.on_final("done2")
    assert cm.updates == ["done2"]


# --- FIX 1: label the driven session's narration when several are live ------------

@pytest.mark.asyncio
async def test_on_final_labels_narration_when_multi_and_label_set():
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    spoken = []
    hub.attach(lambda t: spoken.append(t))
    hub.label_fn = lambda: "loop"
    hub.multi_fn = lambda: True
    await hub.on_final("done: tests pass")
    assert spoken == ["[loop] done: tests pass"]


@pytest.mark.asyncio
async def test_on_final_single_session_stays_bare():
    # Single-session behavior must be byte-identical to today even when label_fn
    # and multi_fn are set, as long as multi_fn() reports only one session live.
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    spoken = []
    hub.attach(lambda t: spoken.append(t))
    hub.label_fn = lambda: "loop"
    hub.multi_fn = lambda: False
    await hub.on_final("done: tests pass")
    assert spoken == ["done: tests pass"]


@pytest.mark.asyncio
async def test_on_final_no_label_fn_stays_bare():
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    spoken = []
    hub.attach(lambda t: spoken.append(t))
    hub.multi_fn = lambda: True
    await hub.on_final("done: tests pass")
    assert spoken == ["done: tests pass"]


@pytest.mark.asyncio
async def test_on_final_empty_label_stays_bare():
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    spoken = []
    hub.attach(lambda t: spoken.append(t))
    hub.label_fn = lambda: ""
    hub.multi_fn = lambda: True
    await hub.on_final("done: tests pass")
    assert spoken == ["done: tests pass"]


@pytest.mark.asyncio
async def test_on_final_label_applies_to_offline_ring_too():
    # When no line is attached, the labeled text goes through the offline ring path
    # the same way as the spoken path.
    cm = FakeCM()
    hub = SessionHub(FakeController(), cm)
    hub.label_fn = lambda: "veil"
    hub.multi_fn = lambda: True
    await hub.on_final("done: tests pass")
    assert cm.updates == ["[veil] done: tests pass"]
