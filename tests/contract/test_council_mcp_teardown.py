"""Council's owned-child teardown and secret redaction (SW-COUNCIL-003, criterion 4).

The Council MCP client must own exactly *its* child: a timeout or a cancellation
tears down only the adapter-owned process group, and a sibling/server process the
caller owns survives untouched. This suite proves it against a real hung server
and a real sibling sentinel process, and it proves that a secret sentinel on the
child's stderr is redacted before the text is ever surfaced.

* the hung call hits the client's own deadline and is a typed timeout;
* after teardown the owned child's pid is gone;
* the sibling sentinel -- started by the *test*, never by the client -- is still
  alive (a teardown that reached beyond the owned process group would kill it);
* the child runs in its own session/process group (``start_new_session``);
* a secret sentinel written to the child's stderr is redacted, and the exact
  secret value never appears in the drained text.

No live provider is called.
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
FAKE = FIX_C / "fake_mcp_server.py"
SENTINEL = FIX_C / "sibling_sentinel.py"

from skillweave.council.mcp_client import (  # noqa: E402
    McpStdioClient,
    McpTimeoutError,
    redact_text,
)


def _fake(mode: str) -> list[str]:
    return [sys.executable, str(FAKE), mode]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _Sibling:
    """A caller-owned sentinel process that must survive the client's teardown."""

    def __init__(self, tmp_path: Path):
        self.pidfile = tmp_path / "sibling.pid"
        self.proc = None
        self.pid = None

    def __enter__(self):
        self.proc = __import__("subprocess").Popen(
            [sys.executable, str(SENTINEL), str(self.pidfile)]
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.pidfile.exists():
                text = self.pidfile.read_text().strip()
                if text:
                    self.pid = int(text)
                    break
            time.sleep(0.02)
        assert self.pid is not None, "sibling sentinel never recorded its pid"
        return self

    def __exit__(self, *exc):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=5)


class TestOwnedChildTeardown:
    def test_timeout_is_typed_and_only_the_owned_child_is_torn_down(self, tmp_path):
        with _Sibling(tmp_path) as sibling:
            async def scenario():
                client = McpStdioClient(_fake("hang"), timeout=0.5)
                await client.start()
                child_pid = client.pid
                try:
                    await client.list_tools()
                    with pytest.raises(McpTimeoutError):
                        await client.call_tool("perplexity_search", {"query": "x"})
                finally:
                    await client.close()
                return child_pid, client

            child_pid, client = asyncio.run(scenario())
            assert not _alive(child_pid), "the owned child must be reaped"
            assert _alive(sibling.pid), "a sibling process must survive the teardown"

    def test_the_child_runs_in_its_own_process_group(self):
        async def scenario():
            client = McpStdioClient(_fake("normal"))
            await client.start()
            try:
                child_pid = client.pid
                return child_pid, os.getpgid(child_pid)
            finally:
                await client.close()

        child_pid, child_pgid = asyncio.run(scenario())
        assert child_pgid == child_pid, "start_new_session makes the child a group leader"
        assert child_pgid != os.getpgid(0), "the child must not share the test's group"

    def test_cancel_reaps_the_owned_child_and_spares_the_sibling(self, tmp_path):
        with _Sibling(tmp_path) as sibling:
            async def scenario():
                client = McpStdioClient(_fake("hang"), timeout=0.5)
                await client.start()
                child_pid = client.pid
                # Cancel before waiting for a response: send notifications/cancelled
                # then tear the owned group down.
                await client.cancel(request_id=1)
                return child_pid

            child_pid = asyncio.run(scenario())
            assert not _alive(child_pid), "cancel must reap the owned child"
            assert _alive(sibling.pid), "cancel must spare the sibling process"

    def test_close_is_idempotent_and_neutral_before_start(self):
        async def scenario():
            client = McpStdioClient(_fake("normal"))
            await client.close()  # never started: a no-op
            await client.start()
            await client.close()
            await client.close()  # second close: still a no-op

        asyncio.run(scenario())


class TestSecretRedaction:
    def test_stderr_secret_is_redacted_in_the_drained_text(self):
        async def scenario():
            client = McpStdioClient(_fake("stderr-secret"))
            await client.start()
            try:
                # Give the stderr drain task a moment to read the banner.
                for _ in range(50):
                    if "[REDACTED]" in client.stderr_text():
                        break
                    await asyncio.sleep(0.02)
                return client.stderr_text()
            finally:
                await client.close()

        text = asyncio.run(scenario())
        assert "[REDACTED]" in text, text
        assert "pplx-FAKESECRET0123456789" not in text, text

    def test_redact_text_masks_known_values_and_shapes(self):
        secret = "pplx-FAKESECRET0123456789"
        blob = (
            f"token {secret}\n"
            "Authorization: Bearer sk-abcdef0123456789\n"
            "api_key=ABCDEF0123456789\n"
        )
        out = redact_text(blob, secrets=[secret])
        assert secret not in out
        assert "sk-abcdef0123456789" not in out
        assert "api_key=ABCDEF0123456789" not in out
        assert out.count("[REDACTED]") >= 3, out

    def test_empty_stderr_is_a_safe_empty_string(self):
        async def scenario():
            client = McpStdioClient(_fake("normal"))
            await client.start()
            try:
                return client.stderr_text()
            finally:
                await client.close()

        assert asyncio.run(scenario()) == ""
