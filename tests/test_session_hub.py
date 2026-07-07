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
