from server.notifier import Notifier


class _FakeCM:
    def __init__(self):
        self.line_open = False
        self.queued: list[str] = []
        self.updates: list[str] = []
        self.last_approval = None

    def queue(self, s, approval=None):
        self.queued.append(s)

    async def on_update(self, s, approval=None):
        self.updates.append(s)
        self.last_approval = approval


async def test_silent_when_line_open():
    cm = _FakeCM()
    cm.line_open = True
    n = Notifier(cm, push_enabled=True)
    await n.report("done")
    assert cm.queued == [] and cm.updates == []


async def test_queues_without_ring_while_app_open():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True)
    n.note_client_connected()
    await n.report("done")
    assert cm.queued == ["done"] and cm.updates == []


async def test_client_count_never_goes_negative():
    n = Notifier(_FakeCM(), push_enabled=True)
    n.note_client_disconnected()
    assert n.phone_clients == 0
    n.note_client_connected()
    n.note_client_disconnected()
    assert n.phone_clients == 0


async def test_rings_when_app_closed():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True)
    await n.report("done")
    assert cm.updates == ["done"] and cm.queued == []


async def test_duplicate_ring_debounced():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=6.0)
    await n.report("first")
    await n.report("ghost")     # same finish reported by hook + scraper
    assert cm.updates == ["first"]


async def test_debounce_zero_allows_both():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    await n.report("a")
    await n.report("b")
    assert cm.updates == ["a", "b"]


async def test_debounce_default_read_from_env(monkeypatch):
    monkeypatch.setenv("VOXA_RING_DEBOUNCE_SECONDS", "0")
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True)
    await n.report("a")
    await n.report("b")
    assert cm.updates == ["a", "b"]


async def test_cloud_ring_only_when_push_disabled():
    cm = _FakeCM()
    rang = []

    async def spy(summary, approval=None):
        rang.append(summary)

    n = Notifier(cm, push_enabled=False, ring_debounce=0)
    n._ring_via_cloud = spy
    await n.report("done")
    assert rang == ["done"]           # zero-config path: cloud does the ring

    n2 = Notifier(cm, push_enabled=True, ring_debounce=0)
    n2._ring_via_cloud = spy
    await n2.report("again")
    assert rang == ["done"]           # local APNs key: on_update already rang


async def test_cloud_paths_are_noops_without_relay_or_account(monkeypatch):
    # Fail-open: with no relay URL or account configured these must simply
    # return, never raise into the caller.
    monkeypatch.delenv("VOXA_RELAY_URL", raising=False)
    n = Notifier(_FakeCM(), push_enabled=False, ring_debounce=0)
    await n._ring_via_cloud("x")
    await n.cancel_via_cloud()


async def test_silent_rule_queues_instead_of_ringing(tmp_path):
    from server.notify_rules import NotifyRules
    rules = NotifyRules(str(tmp_path / "r.json"))
    rules.set_mode("/p/quiet", "finish", "silent")
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0, rules=rules)
    await n.report("quiet finished", kind="finish", cwd="/p/quiet")
    assert cm.queued == ["quiet finished"] and cm.updates == []


async def test_ring_rule_default_still_rings(tmp_path):
    from server.notify_rules import NotifyRules
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0,
                 rules=NotifyRules(str(tmp_path / "r.json")))
    await n.report("loud finished", kind="finish", cwd="/p/loud")
    assert cm.updates == ["loud finished"]


async def test_approval_travels_to_call_manager(tmp_path):
    from server.notify_rules import NotifyRules
    cm = _FakeCM()   # extend _FakeCM: on_update(self, s, approval=None) records approvals
    n = Notifier(cm, push_enabled=True, ring_debounce=0,
                 rules=NotifyRules(str(tmp_path / "r.json")))
    a = {"approval_id": "abc", "cwd": "/p/x", "options": []}
    await n.report("needs input", kind="needs_input", cwd="/p/x", approval=a)
    assert cm.last_approval == a


# --- Task 2: per-item ring suppression while a queue burst is active -------------

async def test_finish_suppressed_while_cwd_in_queue_active_set():
    # A per-item finish for a cwd whose burst is engaged must NOT ring: it is
    # folded into the drain digest instead.
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.queue_active_cwds.add("/p/loop")
    await n.report("loop finished: item 1", kind="finish", cwd="/p/loop")
    assert cm.updates == [] and cm.queued == []


async def test_finish_trailing_slash_normalized_for_suppression():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.queue_active_cwds.add("/p/loop")
    await n.report("done", kind="finish", cwd="/p/loop/")   # trailing slash tolerated
    assert cm.updates == []


async def test_finish_for_other_cwd_still_rings_during_a_burst():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.queue_active_cwds.add("/p/loop")
    await n.report("other done", kind="finish", cwd="/p/other")
    assert cm.updates == ["other done"]


async def test_needs_input_rings_immediately_even_during_a_burst():
    # needs_input is never suppressed: it is the "1 needs you" that must reach the
    # user right away (Phase 1 semantics), and it also notifies the runner to pause.
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.queue_active_cwds.add("/p/loop")
    paused = []
    async def on_needs(cwd): paused.append(cwd)
    n.on_queue_needs_input = on_needs
    await n.report("loop needs input", kind="needs_input", cwd="/p/loop")
    assert cm.updates == ["loop needs input"]   # rang immediately, not folded
    assert paused == ["/p/loop"]                 # runner told to pause


async def test_on_queue_needs_input_failure_is_swallowed():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.queue_active_cwds.add("/p/loop")
    async def boom(cwd): raise RuntimeError("nope")
    n.on_queue_needs_input = boom
    await n.report("loop needs input", kind="needs_input", cwd="/p/loop")  # must not raise
    assert cm.updates == ["loop needs input"]
