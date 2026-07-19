import pytest


@pytest.fixture(autouse=True)
def _quiet_window_ring_for_tests(monkeypatch):
    """Instant ring is the PRODUCT default (ring_policy.py); the bulk of the
    ring tests were written against the quiet-window behavior, so tests default
    it off. Instant-mode tests setenv it back to 1 themselves, and
    test_instant_ring_is_the_default pins the real default by deleting the var."""
    monkeypatch.setenv("VOXA_RING_INSTANT", "0")


@pytest.fixture(autouse=True)
def _isolated_phone_state(tmp_path, monkeypatch):
    """Keep tests away from the developer's real ~/.voxa/phone.json.

    create_app() wires the Notifier's persisted paired-phone identity to
    VOXA_PHONE_STATE_FILE (default ~/.voxa/phone.json); without this fixture
    any test that opens a /ws connection would overwrite the developer's real
    pairing with test fixtures like "user-77"."""
    monkeypatch.setenv("VOXA_PHONE_STATE_FILE", str(tmp_path / "phone.json"))
