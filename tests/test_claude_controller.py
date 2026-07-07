import pytest
from server.claude_controller import ClaudeController, text_from_assistant


# --- fakes that mimic the SDK shapes we depend on ---
class FakeTextBlock:
    def __init__(self, text): self.text = text

class FakeAssistant:
    def __init__(self, *texts): self.content = [FakeTextBlock(t) for t in texts]

class FakeResult:  # marks end of stream
    pass

class FakeSession:
    def __init__(self, messages): self._messages = messages
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def query(self, text): self.queried = text
    async def receive_response(self):
        for m in self._messages:
            yield m

def make_factory(messages):
    def factory(working_dir): return FakeSession(messages)
    return factory


def test_text_from_assistant_joins_blocks():
    assert text_from_assistant(FakeAssistant("hello ", "world")) == "hello world"

def test_text_from_assistant_ignores_non_assistant():
    assert text_from_assistant(FakeResult()) is None


async def test_start_validates_dir(tmp_path):
    c = ClaudeController(session_factory=make_factory([]))
    await c.start(str(tmp_path))
    assert c.working_dir == str(tmp_path)
    assert c.status == "idle"


async def test_session_persists_across_sends(tmp_path):
    created = []
    def factory(wd):
        s = FakeSession([FakeAssistant("ok"), FakeResult()])
        created.append(s)
        return s
    c = ClaudeController(session_factory=factory)
    await c.start(str(tmp_path))
    await c.send("first")
    await c.send("second")
    assert len(created) == 1               # one persistent session, not one-per-send
    assert created[0].queried == "second"  # both queries hit the same session


async def test_watch_log_captures_prompt_and_output(tmp_path):
    log = tmp_path / "watch.log"
    messages = [FakeAssistant("hello "), FakeAssistant("world"), FakeResult()]
    c = ClaudeController(
        session_factory=make_factory(messages),
        watch_log_path=str(log),
        launch_terminal=False,  # never spawn a Terminal in tests
    )
    await c.start(str(tmp_path))
    await c.send("do the thing")
    content = log.read_text()
    assert "do the thing" in content   # prompt header
    assert "hello" in content          # streamed assistant output
    assert "world" in content
    assert "✓ done" in content         # completion marker

async def test_start_rejects_missing_dir():
    c = ClaudeController(session_factory=make_factory([]))
    with pytest.raises(ValueError):
        await c.start("/no/such/dir/here")


async def test_send_runs_and_fires_final(tmp_path):
    messages = [FakeAssistant("partial"), FakeAssistant("final answer"), FakeResult()]
    c = ClaudeController(session_factory=make_factory(messages))
    captured = []
    c.on_final(lambda text: captured.append(text))   # sync callback allowed
    await c.start(str(tmp_path))
    await c.send("do the thing")
    assert c.status == "finished"
    assert captured == ["final answer"]

async def test_send_sets_error_on_exception(tmp_path):
    class Boom(FakeSession):
        async def receive_response(self):
            raise RuntimeError("kaboom")
            yield  # pragma: no cover
    c = ClaudeController(session_factory=lambda wd: Boom([]))
    await c.start(str(tmp_path))
    await c.send("x")
    assert c.status == "error"
