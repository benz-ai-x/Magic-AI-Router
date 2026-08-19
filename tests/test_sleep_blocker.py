"""Tests for _should_prevent_sleep gating + CaffeinateBlocker lifecycle."""
import os
from unittest.mock import MagicMock, patch

import pytest

from sysctl import sleep_blocker
from services.lifecycle_runtime import _should_prevent_sleep


@pytest.mark.parametrize(
    ("status", "paused", "flag", "expected"),
    [
        ("connected", False, True, True),    # the one True case
        ("connected", False, False, False),   # opt-in off
        ("connected", True, True, False),     # paused → release
        ("connecting", False, True, False),
        ("stopped", False, True, False),
        ("error", False, True, False),
        ("", False, True, False),
    ],
)
def test_should_prevent_sleep_truth_table(status, paused, flag, expected):
    assert _should_prevent_sleep(status, paused, flag) is expected


def _fake_proc():
    proc = MagicMock()
    proc.poll.return_value = None  # still running
    return proc


def test_acquire_starts_caffeinate_with_is_and_w_pid():
    blocker = sleep_blocker.CaffeinateBlocker(bin_path="/usr/bin/caffeinate")
    with patch("sysctl.sleep_blocker.subprocess.Popen") as popen:
        popen.return_value = _fake_proc()
        blocker.acquire()
    args = popen.call_args[0][0]
    assert args[0] == "/usr/bin/caffeinate"
    assert "-i" in args and "-s" in args
    assert "-w" in args
    w_idx = args.index("-w")
    assert args[w_idx + 1] == str(os.getpid())


def test_acquire_idempotent_when_running():
    blocker = sleep_blocker.CaffeinateBlocker()
    with patch("sysctl.sleep_blocker.subprocess.Popen") as popen:
        popen.return_value = _fake_proc()
        blocker.acquire()
        blocker.acquire()  # second call must not spawn again
    assert popen.call_count == 1


def test_acquire_failure_is_non_fatal_and_latches():
    blocker = sleep_blocker.CaffeinateBlocker()
    with patch("sysctl.sleep_blocker.subprocess.Popen", side_effect=OSError("nope")):
        blocker.acquire()  # logs once, stays not-running
        blocker.acquire()  # second failure must not log again (latched)
    assert blocker.is_running is False


def test_release_terminates_running_proc():
    blocker = sleep_blocker.CaffeinateBlocker()
    proc = _fake_proc()
    with patch("sysctl.sleep_blocker.subprocess.Popen", return_value=proc):
        blocker.acquire()
    blocker.release()
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once()


def test_release_idempotent_when_not_running():
    blocker = sleep_blocker.CaffeinateBlocker()
    blocker.release()  # no proc → no error
    blocker.release()


def test_release_clears_running_state():
    blocker = sleep_blocker.CaffeinateBlocker()
    with patch("sysctl.sleep_blocker.subprocess.Popen", return_value=_fake_proc()):
        blocker.acquire()
    assert blocker.is_running is True
    blocker.release()
    assert blocker.is_running is False
