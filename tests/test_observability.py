from server.observability import init_sentry


def test_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry("test") is False


def test_never_raises_with_dsn_but_no_sdk(monkeypatch):
    # sentry-sdk is an optional extra; a configured DSN without the package must
    # log-and-continue, not crash the server at boot.
    monkeypatch.setenv("SENTRY_DSN", "https://x@example.invalid/1")
    import sys
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)   # forces ImportError on import
    assert init_sentry("test") is False
