"""Unit tests for the async run lifecycle (SW-159P-ASYNC-001).

Proves the three CLI surfaces and the backing store/handle layer:

* ``start_async`` returns a ``RunHandle`` with a live child PID; the child
  survives the launcher exit and writes structured events/state.
* ``inspect_run`` reads back run state (snapshot) or streams events (follow).
* ``kill_run`` is idempotent: graceful (SIGTERM) then forced (SIGKILL), with
  stale-PID protection.

The hermetic boundary is inherited from the sibling unit tests: no outbound
socket, provider env vars cleared.  The dispatched child is a short-lived
``python -c`` fragment that produces structured events.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from skillweave.runlifecycle.handle import (
    RunHandle,
    InspectMode,
    start_async,
    inspect_run,
    kill_run,
)
from skillweave.runlifecycle.store import (
    RunMetadata,
    RunStateStore,
    StalePidError,
    RunStateError,
)

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _child_command() -> list[str]:
    """Return a short-lived child command that exits cleanly."""
    return [
        sys.executable,
        "-c",
        "import sys; print('CHILD-LINE-1', flush=True); sys.exit(0)",
    ]


def _child_sleep_command(seconds: float = 10) -> list[str]:
    """Return a child command that sleeps for *seconds*."""
    return [
        sys.executable,
        "-c",
        f"import time; print('sleeping', flush=True); time.sleep({seconds}); print('done', flush=True)",
    ]


def _wait_for_state(
    store: RunStateStore, run_id: str, state: str, timeout: float = 5.0
) -> RunMetadata:
    """Wait until *run_id* reaches *state*, polling the store."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = store.read_metadata(run_id)
        if meta and meta.state == state:
            return meta
        time.sleep(0.02)
    raise AssertionError(
        f"run {run_id} never reached state {state!r} "
        f"(last: {meta.state if meta else 'None'})"
    )


def _wait_for_events(
    store: RunStateStore, run_id: str, min_count: int, timeout: float = 5.0
) -> list[dict]:
    """Wait until *run_id* has at least *min_count* events."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = store.read_events(run_id)
        if len(events) >= min_count:
            return events
        time.sleep(0.02)
    raise AssertionError(
        f"run {run_id} never reached {min_count} events "
        f"(last count: {len(events)})"
    )


# ---------------------------------------------------------------------------
# RunStateStore
# ---------------------------------------------------------------------------


class TestRunStateStore:
    def test_write_and_read_metadata(self, tmp_path):
        store = RunStateStore(tmp_path)
        meta = RunMetadata.create(
            run_id="abc123", command=["echo", "hello"], pid=42
        )
        store.write_metadata(meta)

        read = store.read_metadata("abc123")
        assert read is not None
        assert read.run_id == "abc123"
        assert read.command == ["echo", "hello"]
        assert read.pid == 42
        assert read.state == "running"
        assert read.version == 1

    def test_update_metadata_bumps_version(self, tmp_path):
        store = RunStateStore(tmp_path)
        meta = RunMetadata.create(run_id="abc123", command=["true"], pid=99)
        store.write_metadata(meta)

        store.update_metadata("abc123", state="succeeded", exit_code=0)
        read = store.read_metadata("abc123")
        assert read is not None
        assert read.state == "succeeded"
        assert read.exit_code == 0
        assert read.version == 2

    def test_read_metadata_missing(self, tmp_path):
        store = RunStateStore(tmp_path)
        assert store.read_metadata("no-such-id") is None

    def test_append_and_read_events(self, tmp_path):
        store = RunStateStore(tmp_path)
        store.append_event("abc123", {"event": "launched", "sequence": 1})
        store.append_event("abc123", {"event": "started", "sequence": 2})

        events = store.read_events("abc123")
        assert len(events) == 2
        assert events[0]["event"] == "launched"
        assert events[1]["event"] == "started"

    def test_read_events_after_sequence(self, tmp_path):
        store = RunStateStore(tmp_path)
        store.append_event("abc123", {"event": "launched", "sequence": 1})
        store.append_event("abc123", {"event": "started", "sequence": 2})
        store.append_event("abc123", {"event": "finished", "sequence": 3})

        events = store.read_events("abc123", after=1)
        assert len(events) == 2
        assert events[0]["event"] == "started"
        assert events[1]["event"] == "finished"

    def test_latest_event_sequence(self, tmp_path):
        store = RunStateStore(tmp_path)
        assert store.latest_event_sequence("abc123") == 0

        store.append_event("abc123", {"event": "launched"})
        assert store.latest_event_sequence("abc123") == 1

        store.append_event("abc123", {"event": "started"})
        assert store.latest_event_sequence("abc123") == 2

    def test_verify_pid_matches(self, tmp_path):
        store = RunStateStore(tmp_path)
        meta = RunMetadata.create(run_id="abc123", command=["true"], pid=99)
        store.write_metadata(meta)

        # Should not raise
        store.verify_pid("abc123", 99)

    def test_verify_pid_mismatch_raises(self, tmp_path):
        store = RunStateStore(tmp_path)
        meta = RunMetadata.create(run_id="abc123", command=["true"], pid=99)
        store.write_metadata(meta)

        with pytest.raises(StalePidError):
            store.verify_pid("abc123", 100)

    def test_verify_pid_no_run_raises(self, tmp_path):
        store = RunStateStore(tmp_path)
        with pytest.raises(RunStateError, match="no run found"):
            store.verify_pid("no-such-id", 42)

    def test_list_runs_newest_first(self, tmp_path):
        store = RunStateStore(tmp_path)
        meta1 = RunMetadata.create(run_id="aaa", command=["true"], pid=1)
        meta2 = RunMetadata.create(run_id="bbb", command=["true"], pid=2)
        store.write_metadata(meta1)
        time.sleep(0.01)
        store.write_metadata(meta2)

        runs = store.list_runs()
        assert len(runs) >= 2
        assert runs[0] == "bbb"  # newest first


# ---------------------------------------------------------------------------
# start_async
# ---------------------------------------------------------------------------


class TestStartAsync:
    def test_returns_handle_with_live_child(self, tmp_path):
        cmd = _child_sleep_command(2)
        handle = start_async(command=cmd, project_root=tmp_path)

        assert isinstance(handle, RunHandle)
        assert handle.run_id
        assert isinstance(handle.pid, int)
        assert handle.pid > 0
        assert handle.command == cmd
        assert handle.state == "running"

        # The handle was returned synchronously (child still alive)
        try:
            os.kill(handle.pid, 0)  # check existence
        except ProcessLookupError:
            raise AssertionError("child died before handle was returned")

        # Metadata was persisted
        store = RunStateStore(tmp_path)
        meta = store.read_metadata(handle.run_id)
        assert meta is not None
        assert meta.state == "running"
        assert meta.pid == handle.pid

        # Launch event was written
        events = store.read_events(handle.run_id)
        assert len(events) >= 1
        assert events[0]["event"] == "launched"

    def test_child_completes_and_writes_state(self, tmp_path):
        handle = start_async(
            command=_child_command(), project_root=tmp_path
        )

        store = RunStateStore(tmp_path)
        meta = _wait_for_state(store, handle.run_id, "succeeded")

        assert meta.exit_code == 0
        assert meta.finished_at

        # Events include started and finished
        events = store.read_events(handle.run_id)
        event_types = [e["event"] for e in events]
        assert "started" in event_types
        assert "finished" in event_types

    def test_explicit_run_id(self, tmp_path):
        handle = start_async(
            command=_child_command(),
            project_root=tmp_path,
            run_id="my-custom-id",
        )
        assert handle.run_id == "my-custom-id"

        store = RunStateStore(tmp_path)
        meta = store.read_metadata("my-custom-id")
        assert meta is not None


# ---------------------------------------------------------------------------
# inspect_run
# ---------------------------------------------------------------------------


class TestInspectRun:
    def test_snapshot_returns_state(self, tmp_path):
        handle = start_async(
            command=_child_sleep_command(5), project_root=tmp_path
        )

        buf = io.StringIO()
        code = inspect_run(
            handle.run_id, project_root=tmp_path, out=buf
        )
        assert code == 0

        data = json.loads(buf.getvalue())
        assert data["run_id"] == handle.run_id
        assert data["pid"] == handle.pid
        assert data["state"] == "running"
        assert "lane_states" in data
        assert "event_count" in data

    def test_snapshot_missing_run(self, tmp_path):
        buf = io.StringIO()
        code = inspect_run("no-such-id", project_root=tmp_path, out=buf)
        assert code == 1

        data = json.loads(buf.getvalue())
        assert "error" in data

    def test_follow_streams_events(self, tmp_path):
        handle = start_async(
            command=_child_command(), project_root=tmp_path
        )

        buf = io.StringIO()
        code = inspect_run(
            handle.run_id,
            project_root=tmp_path,
            mode=InspectMode.FOLLOW,
            poll_interval=0.02,
            timeout=5.0,
            out=buf,
        )
        assert code == 0

        output = buf.getvalue()
        assert handle.run_id in output
        assert "started" in output
        assert "finished" in output

    def test_follow_timeout(self, tmp_path):
        handle = start_async(
            command=_child_sleep_command(30), project_root=tmp_path
        )

        buf = io.StringIO()
        code = inspect_run(
            handle.run_id,
            project_root=tmp_path,
            mode=InspectMode.FOLLOW,
            poll_interval=0.02,
            timeout=0.1,
            out=buf,
        )
        assert code == 1

        output = buf.getvalue()
        assert "timeout" in output


# ---------------------------------------------------------------------------
# kill_run
# ---------------------------------------------------------------------------


class TestKillRun:
    def test_kill_terminates_child(self, tmp_path):
        handle = start_async(
            command=_child_sleep_command(30), project_root=tmp_path
        )

        result = kill_run(
            run_id=handle.run_id, pid=handle.pid, project_root=tmp_path
        )

        assert result["run_id"] == handle.run_id
        assert result["pid"] == handle.pid
        assert result["state"] == "killed"
        assert result["result"] in ("graceful_kill", "forced_kill")

        # Metadata updated
        store = RunStateStore(tmp_path)
        meta = store.read_metadata(handle.run_id)
        assert meta is not None
        assert meta.state == "killed"

        # Kill event was written
        events = store.read_events(handle.run_id)
        kill_events = [e for e in events if e.get("event") == "killed"]
        assert len(kill_events) == 1
        assert kill_events[0]["result"] in ("graceful_kill", "forced_kill")

    def test_kill_already_terminal_is_idempotent(self, tmp_path):
        handle = start_async(
            command=_child_command(), project_root=tmp_path
        )

        store = RunStateStore(tmp_path)
        _wait_for_state(store, handle.run_id, "succeeded")

        result = kill_run(
            run_id=handle.run_id, pid=handle.pid, project_root=tmp_path
        )
        assert result["state"] == "succeeded"
        assert result["result"] == "already_terminal"

    def test_kill_stale_pid_raises(self, tmp_path):
        handle = start_async(
            command=_child_command(), project_root=tmp_path
        )

        store = RunStateStore(tmp_path)
        _wait_for_state(store, handle.run_id, "succeeded")

        with pytest.raises(StalePidError):
            kill_run(
                run_id=handle.run_id,
                pid=999999,
                project_root=tmp_path,
            )

    def test_kill_no_such_run(self, tmp_path):
        with pytest.raises(RunStateError, match="no run found"):
            kill_run(run_id="no-such-id", pid=42, project_root=tmp_path)

    def test_kill_records_durable_intent_before_signal(self, tmp_path, monkeypatch):
        """The facade records a cancellation intent before the signal boundary.

        If the facade is lost at the signal call, the intent survives so a
        later production resume can finish the cancellation.
        """
        import skillweave.runlifecycle.handle as handle_module

        handle = start_async(command=_child_sleep_command(30), project_root=tmp_path)
        store = RunStateStore(tmp_path)

        seen: dict = {}

        def crash_at_signal(*_a, **_k):
            record = store._kernel.get_run(handle.run_id)
            seen["pending"] = record.metadata.get("_pending_cancellation")
            raise KeyboardInterrupt("facade lost at the signal boundary")

        monkeypatch.setattr(handle_module.os, "killpg", crash_at_signal)
        monkeypatch.setattr(handle_module.os, "kill", crash_at_signal)
        with pytest.raises(KeyboardInterrupt):
            kill_run(run_id=handle.run_id, pid=handle.pid, project_root=tmp_path)

        assert isinstance(seen.get("pending"), dict), (
            "no durable cancellation intent existed before the facade signal")
        assert seen["pending"].get("idempotency_key") == "kill:" + handle.run_id


# ---------------------------------------------------------------------------
# RunHandle serialisation
# ---------------------------------------------------------------------------


class TestRunHandle:
    def test_to_dict_roundtrip(self):
        handle = RunHandle(
            run_id="abc123",
            pid=42,
            command=["echo", "hello"],
            state="running",
            created_at="2024-01-01T00:00:00",
            version=1,
        )
        data = handle.to_dict()
        assert data["run_id"] == "abc123"
        assert data["pid"] == 42

        restored = RunHandle.from_dict(data)
        assert restored.run_id == "abc123"
        assert restored.pid == 42
        assert restored.command == ["echo", "hello"]
        assert restored.state == "running"


# ---------------------------------------------------------------------------
# CLI parsers
# ---------------------------------------------------------------------------


class TestCLIParsers:
    def test_start_parser_defaults(self):
        from skillweave.runlifecycle.cli import build_start_parser

        parser = build_start_parser()
        args = parser.parse_args(["--", "echo", "hello"])
        # nargs=REMAINDER includes the -- separator.  The positional is named
        # ``start_argv`` (not ``command``) so it cannot overwrite the router's
        # ``add_subparsers(dest="command")`` value when mounted as a parent.
        assert args.start_argv == ["--", "echo", "hello"]
        assert args.async_run is False
        assert args.run_id is None

    def test_start_parser_async_flag(self):
        from skillweave.runlifecycle.cli import build_start_parser

        parser = build_start_parser()
        args = parser.parse_args(["--async", "--", "echo", "hello"])
        assert args.async_run is True
        assert args.start_argv == ["--", "echo", "hello"]

    def test_start_parser_explicit_run_id(self):
        from skillweave.runlifecycle.cli import build_start_parser

        parser = build_start_parser()
        args = parser.parse_args(
            ["--async", "--run-id", "my-id", "--", "echo", "hello"]
        )
        assert args.run_id == "my-id"

    def test_inspect_parser_defaults(self):
        from skillweave.runlifecycle.cli import build_inspect_parser

        parser = build_inspect_parser()
        args = parser.parse_args(["abc123"])
        assert args.run_id == "abc123"
        assert args.follow is False
        assert args.poll_interval == 0.25
        assert args.timeout is None

    def test_inspect_parser_follow(self):
        from skillweave.runlifecycle.cli import build_inspect_parser

        parser = build_inspect_parser()
        args = parser.parse_args(["abc123", "--follow"])
        assert args.follow is True

    def test_kill_parser(self):
        from skillweave.runlifecycle.cli import build_kill_parser

        parser = build_kill_parser()
        args = parser.parse_args(["abc123", "42"])
        assert args.run_id == "abc123"
        assert args.pid == 42
