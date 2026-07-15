import base64
from unittest.mock import patch, MagicMock

from server.screenshot import capture_screenshot


def _fake_run_success(cmd, **kwargs):
    # screencapture writes to the path given as the last argument; sips edits in
    # place and doesn't need to do anything for this fake.
    if cmd[0] == "screencapture":
        with open(cmd[-1], "wb") as f:
            f.write(b"fake-jpeg-bytes")
    return MagicMock(returncode=0)


async def test_capture_screenshot_success():
    with patch("server.screenshot.subprocess.run", side_effect=_fake_run_success):
        result = await capture_screenshot()
    assert "error" not in result
    assert base64.b64decode(result["image"]) == b"fake-jpeg-bytes"


def _fake_run_empty(cmd, **kwargs):
    # screencapture "succeeds" (exit 0) but never writes the file: this is what
    # happens when macOS Screen Recording permission is missing.
    return MagicMock(returncode=0)


async def test_capture_screenshot_permission_denied():
    with patch("server.screenshot.subprocess.run", side_effect=_fake_run_empty):
        result = await capture_screenshot()
    assert "image" not in result
    assert "Screen Recording" in result["error"]


def _fake_run_raises(cmd, **kwargs):
    raise OSError("screencapture: command not found")


async def test_capture_screenshot_subprocess_error():
    with patch("server.screenshot.subprocess.run", side_effect=_fake_run_raises):
        result = await capture_screenshot()
    assert "image" not in result
    assert "error" in result


def _fake_run_sips_fails(cmd, **kwargs):
    # screencapture succeeds and writes the file, but sips fails.
    if cmd[0] == "screencapture":
        with open(cmd[-1], "wb") as f:
            f.write(b"fake-jpeg-bytes")
        return MagicMock(returncode=0)
    elif cmd[0] == "sips":
        # sips fails with a non-zero exit code.
        return MagicMock(returncode=1)
    return MagicMock(returncode=0)


async def test_capture_screenshot_sips_fails():
    with patch("server.screenshot.subprocess.run", side_effect=_fake_run_sips_fails):
        result = await capture_screenshot()
    assert "image" not in result
    assert "error" in result
    assert "sips" in result["error"]


async def test_capture_screenshot_mkstemp_fails():
    def _fake_mkstemp(*args, **kwargs):
        raise OSError("no space left on device")

    with patch("server.screenshot.tempfile.mkstemp", side_effect=_fake_mkstemp):
        result = await capture_screenshot()
    assert "image" not in result
    assert "error" in result
