"""UsageSink 与失败路径韧性（issue #15）.

usage write/rotate 的任何 OSError 都不改变上游响应或 502 状态；
目录/文件强制 0700/0600 并拒 symlink；rolling 只在落盘成功后更新；
失败指标可观测且节流记录。
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from suanpan.usage_log import UsageEntry, UsageLogger


def _entry(status=502):
    return UsageEntry(provider="p", source_model="m", target_model="m2",
                      scenario="s", input_tokens=1, output_tokens=2,
                      cache_read_tokens=0, cache_creation_tokens=0,
                      latency_ms=10, status=status, error="x")


class TestWriteNeverBreaksResponse(unittest.TestCase):
    def test_write_oserror_swallowed_and_counted(self):
        with tempfile.TemporaryDirectory() as d:
            log = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            with patch("suanpan.usage_log.os.open", side_effect=OSError("disk full")):
                with self.assertLogs("magic-proxy.suanpan.usage", level="WARNING"):
                    log.write(_entry())  # 必须不抛
            self.assertEqual(log.write_failures, 1, "失败指标可观测")
            self.assertEqual(log.rolling["calls"], 0,
                             "落盘失败时 rolling 不更新")

    def test_502_response_unchanged_by_write_failure(self):
        from suanpan.proxy import make_502
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            log = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            with patch("suanpan.usage_log.os.open", side_effect=OSError("read-only")):
                resp = make_502("p", "m", "m2", "s", "err", 0.0, log)
        self.assertEqual(resp.status_code, 502,
                         "写失败不得遮蔽 502 状态")
        body = _json.loads(resp.body.decode())
        self.assertIsInstance(body, dict)  # 502 结构存在（写失败不改变）


class TestPermissions(unittest.TestCase):
    def test_dir_0700_file_0600_and_symlink_rejected(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory(dir=home) as d:
            path = str(Path(d) / "cap" / "u.jsonl")
            log = UsageLogger(enabled=True, path=path)
            log.write(_entry())
            dirmode = stat.S_IMODE(os.stat(Path(path).parent).st_mode)
            filemode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(dirmode, 0o700)
            self.assertEqual(filemode, 0o600)
            # symlink 拒写
            link = Path(d) / "link.jsonl"
            os.symlink(path, link)
            log2 = UsageLogger(enabled=True, path=str(link))
            with self.assertLogs("magic-proxy.suanpan.usage", level="WARNING"):
                log2.write(_entry())  # 不抛；记录失败
            self.assertEqual(log2.write_failures, 1)


class TestRollingGatedOnPersistence(unittest.TestCase):
    def test_rolling_only_after_successful_write(self):
        with tempfile.TemporaryDirectory() as d:
            log = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            with patch("suanpan.usage_log.os.open", side_effect=OSError("disk full")):
                log.write(_entry(status=200))
            self.assertEqual(log.rolling["calls"], 0)
            # 恢复后正常累计
            log.write(_entry(status=200))
            self.assertEqual(log.rolling["calls"], 1)


if __name__ == "__main__":
    unittest.main()
