"""Crash reporting, strictly opt-in: initializes Sentry only when SENTRY_DSN is
set AND the optional sentry-sdk extra is installed. Self-hosters who set neither
get zero telemetry. Never raises: observability must not take the server down.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def init_sentry(service: str) -> bool:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("VOXA_ENV", "production"),
            # Errors only: no performance tracing, no PII, no request bodies.
            traces_sample_rate=0.0,
            send_default_pii=False,
            server_name=service,
        )
        return True
    except Exception:
        logger.warning("sentry init failed; continuing without it", exc_info=True)
        return False
