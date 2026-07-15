"""Capture the Mac's main display and return it as a base64-encoded JPEG."""
import asyncio
import base64
import os
import subprocess
import tempfile

MAX_DIMENSION = 1600
CAPTURE_TIMEOUT = 10

PERMISSION_ERROR = (
    "Screen Recording permission not granted for the Voxa server. Enable it "
    "in System Settings > Privacy & Security > Screen Recording, then try again."
)


async def capture_screenshot() -> dict:
    """Capture the main display, downscale it, and base64-encode it.

    Returns {"image": "<base64 jpeg>"} on success or {"error": "<message>"}
    on failure. Runs in a thread since screencapture/sips are blocking CLIs.
    """
    return await asyncio.to_thread(_capture_screenshot_sync)


def _capture_screenshot_sync() -> dict:
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        # No -D flag: screencapture's documented default is the main display,
        # which is what we want regardless of how many monitors are attached.
        capture = subprocess.run(
            ["screencapture", "-x", "-t", "jpg", path],
            capture_output=True, timeout=CAPTURE_TIMEOUT,
        )
        if (capture.returncode != 0 or not os.path.exists(path)
                or os.path.getsize(path) == 0):
            return {"error": PERMISSION_ERROR}
        # Downscale in place; a Retina capture can otherwise be several MB.
        sips = subprocess.run(
            ["sips", "-Z", str(MAX_DIMENSION), path],
            capture_output=True, timeout=CAPTURE_TIMEOUT,
        )
        if sips.returncode != 0:
            return {"error": "Screenshot capture failed: sips downscale failed"}
        with open(path, "rb") as f:
            data = f.read()
        return {"image": base64.b64encode(data).decode("ascii")}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"Screenshot capture failed: {e}"}
    finally:
        if path is not None and os.path.exists(path):
            os.remove(path)
