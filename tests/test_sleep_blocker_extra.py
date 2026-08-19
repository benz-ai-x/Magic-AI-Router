"""Tests for sleep_blocker.py — CaffeinateBlocker lifecycle."""
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from sysctl import sleep_blocker
class TestCaffeinateBlocker(unittest.TestCase):
    def test_not_running_on_init(self):
        b = sleep_blocker.CaffeinateBlocker()
        self.assertFalse(b.is_running)

    @patch("subprocess.Popen")
    def test_acquire_starts_process(self, mock_popen):
        b = sleep_blocker.CaffeinateBlocker(bin_path="/usr/bin/caffeinate")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        b.acquire()
        self.assertTrue(b.is_running)
        mock_popen.assert_called_once()

    @patch("subprocess.Popen")
    def test_acquire_is_idempotent(self, mock_popen):
        b = sleep_blocker.CaffeinateBlocker(bin_path="/usr/bin/caffeinate")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        b.acquire()
        b.acquire()
        self.assertEqual(mock_popen.call_count, 1)

    def test_acquire_without_binary_does_not_crash(self):
        b = sleep_blocker.CaffeinateBlocker(bin_path="/nonexistent/caffeinate")
        b.acquire()
        self.assertFalse(b.is_running)

    def test_release_when_not_running_is_noop(self):
        b = sleep_blocker.CaffeinateBlocker()
        b.release()
        self.assertFalse(b.is_running)

    @patch("subprocess.Popen")
    def test_release_terminates_process(self, mock_popen):
        b = sleep_blocker.CaffeinateBlocker(bin_path="/usr/bin/caffeinate")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        b.acquire()
        b.release()
        mock_proc.terminate.assert_called_once()
        self.assertFalse(b.is_running)
