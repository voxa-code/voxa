import pytest


@pytest.fixture(autouse=True)
def _isolated_phone_state(tmp_path, monkeypatch):
    """Keep tests away from the developer's real ~/.voxa/phone.json.

    create_app() wires the Notifier's persisted paired-phone identity to
    VOXA_PHONE_STATE_FILE (default ~/.voxa/phone.json); without this fixture
    any test that opens a /ws connection would overwrite the developer's real
    pairing with test fixtures like "user-77"."""
    monkeypatch.setenv("VOXA_PHONE_STATE_FILE", str(tmp_path / "phone.json"))
