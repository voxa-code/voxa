import asyncio

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


async def test_queues_and_still_rings_while_app_open():
    # A terminals-first app is "connected" whenever it's simply open, so no
    # longer swallow the ring: queue the update (so the UI can render it) AND
    # still fall through to the normal ring path.
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.note_client_connected()
    await n.report("done")
    assert cm.queued == ["done"] and cm.updates == ["done"]


async def test_silent_rule_queues_without_ring_even_while_app_open(tmp_path):
    # The per-cwd "silent" rule still queues-without-ringing regardless of
    # phone_clients; it must be checked BEFORE the app-open queue-and-continue
    # so it keeps short-circuiting.
    from server.notify_rules import NotifyRules
    rules = NotifyRules(str(tmp_path / "r.json"))
    rules.set_mode("/p/quiet", "finish", "silent")
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0, rules=rules)
    n.note_client_connected()
    await n.report("quiet finished", kind="finish", cwd="/p/quiet")
    assert cm.queued == ["quiet finished"] and cm.updates == []


async def test_needs_input_rings_while_app_open():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    n.note_client_connected()
    await n.report("needs input", kind="needs_input", cwd="/p/x")
    assert cm.queued == ["needs input"] and cm.updates == ["needs input"]


async def test_debounce_and_prewarm_still_apply_when_app_open():
    # Ring debounce and the prewarm kick are unchanged and must still apply to
    # these newly-allowed rings.
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=6.0)
    n.note_client_connected()
    await n.report("first")
    await n.report("ghost")     # same finish reported by hook + scraper
    assert cm.updates == ["first"]
    assert cm.queued == ["first", "ghost"]   # both still queued for the UI


async def test_line_open_still_unchanged_when_phone_clients_set():
    # A live call must still be silent on the ring path (unchanged), even
    # though phone_clients > 0 in that state too.
    cm = _FakeCM()
    cm.line_open = True
    n = Notifier(cm, push_enabled=True)
    n.note_client_connected()
    await n.report("done")
    assert cm.queued == [] and cm.updates == []


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


# --- Prewarm: report() kicks the warm-greeting path while the phone rings --------

class _FakePrewarmer:
    def __init__(self, on=True):
        self._on = on
        self.started: list[tuple] = []

    def enabled(self):
        return self._on

    async def start(self, summary, cwd, approval):
        self.started.append((summary, cwd, approval))


async def test_report_kicks_prewarmer_when_enabled():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    pw = _FakePrewarmer(on=True)
    n.prewarmer = pw
    approval = {"approval_id": "x"}
    await n.report("loop finished", cwd="/p/loop", approval=approval)
    for _ in range(10):
        await asyncio.sleep(0)   # let the fire-and-forget ensure_future task run
    assert pw.started == [("loop finished", "/p/loop", approval)]


async def test_report_does_not_kick_prewarmer_when_disabled():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    pw = _FakePrewarmer(on=False)
    n.prewarmer = pw
    await n.report("loop finished", cwd="/p/loop")
    for _ in range(10):
        await asyncio.sleep(0)
    assert pw.started == []


async def test_report_without_prewarmer_set_is_a_safe_noop():
    cm = _FakeCM()
    n = Notifier(cm, push_enabled=True, ring_debounce=0)
    assert n.prewarmer is None
    await n.report("loop finished", cwd="/p/loop")   # must not raise


# --- cancel_ring: instant-ring mode's stop-the-still-ringing-phone push -----

async def test_cancel_ring_routes_to_call_manager_when_push_enabled():
    cm = _FakeCM()
    cancelled = []

    async def fake_cancel(account=None):
        cancelled.append(account)
    cm.cancel = fake_cancel

    n = Notifier(cm, push_enabled=True)
    n.last_account = "acct1"
    await n.cancel_ring()
    assert cancelled == ["acct1"]


async def test_cancel_ring_routes_to_cloud_when_push_disabled(monkeypatch):
    n = Notifier(_FakeCM(), push_enabled=False)
    called = []

    async def spy():
        called.append(True)
    n.cancel_via_cloud = spy
    await n.cancel_ring()
    assert called == [True]


async def test_cancel_ring_discards_the_warm_slot():
    # A cancelled ring will never be answered, so the session warmed for it
    # must be torn down instead of idling out its TTL (metered minutes in
    # proxy mode).
    cm = _FakeCM()

    async def fake_cancel(account=None):
        pass
    cm.cancel = fake_cancel

    class _FakePrewarmer:
        def __init__(self):
            self.discarded = 0

        def enabled(self):
            return True

        async def discard(self):
            self.discarded += 1

    n = Notifier(cm, push_enabled=True)
    n.prewarmer = _FakePrewarmer()
    await n.cancel_ring()
    assert n.prewarmer.discarded == 1


# --- FIX 2: foreign finishes spoken live during a call, with their project name --

async def test_report_line_open_speaks_foreign_update_and_still_returns():
    cm = _FakeCM()
    cm.line_open = True
    n = Notifier(cm, push_enabled=True)
    spoken = []

    async def on_update_speak(summary, cwd):
        spoken.append((summary, cwd))
    n.on_update_speak = on_update_speak
    await n.report("veil finished: tests pass", cwd="/p/veil")
    assert spoken == [("veil finished: tests pass", "/p/veil")]
    # Still returns without queueing or ringing (the driven line narrates itself).
    assert cm.queued == [] and cm.updates == []


async def test_report_line_open_without_callback_behaves_like_today():
    cm = _FakeCM()
    cm.line_open = True
    n = Notifier(cm, push_enabled=True)
    assert n.on_update_speak is None
    await n.report("done")
    assert cm.queued == [] and cm.updates == []


async def test_report_line_open_no_summary_does_not_call_on_update_speak():
    cm = _FakeCM()
    cm.line_open = True
    n = Notifier(cm, push_enabled=True)
    spoken = []

    async def on_update_speak(summary, cwd):
        spoken.append((summary, cwd))
    n.on_update_speak = on_update_speak
    await n.report("", cwd="/p/veil")
    assert spoken == []
    assert cm.queued == [] and cm.updates == []


async def test_report_line_open_on_update_speak_failure_is_swallowed():
    cm = _FakeCM()
    cm.line_open = True
    n = Notifier(cm, push_enabled=True)

    async def boom(summary, cwd):
        raise RuntimeError("nope")
    n.on_update_speak = boom
    await n.report("veil finished", cwd="/p/veil")   # must not raise
    assert cm.queued == [] and cm.updates == []
