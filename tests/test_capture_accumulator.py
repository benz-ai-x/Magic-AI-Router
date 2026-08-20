"""CaptureAccumulator（issue #11）：每 flow 单一聚合内存预算.

流量字节原样立即下发；tee 收集受总预算约束（request 快照 + response
累计共享同一预算）；大量小 messages/blocks 也无法绕过（按累计字节计，
不按条目数）。truncated/captured_bytes 语义对 UTF-8 安全（不切多字节）。
"""
import pathlib
import unittest

from capture.ai_capture_addon import CaptureAccumulator


class TestBudget(unittest.TestCase):
    def test_default_tee_passes_bytes_through_unchanged(self):
        acc = CaptureAccumulator()
        chunk = b"data: hello\n\n"
        out = acc.tee(chunk)
        self.assertEqual(out, chunk, "流量字节原样立即下发")

    def test_single_aggregate_budget_request_plus_response(self):
        acc = CaptureAccumulator(budget=100)
        acc.reserve_request_snapshot(b"x" * 60)   # 请求快照占 60
        out = acc.tee(b"y" * 60)                  # 响应只剩 40 预算
        self.assertEqual(out, b"y" * 60, "下发不受预算影响")
        self.assertEqual(len(acc.captured()), 40, "响应缓冲拿剩余预算")
        self.assertEqual(acc.total_budgeted(), 100, "聚合（快照+响应）恰好停在预算")
        self.assertTrue(acc.truncated)

    def test_many_small_messages_cannot_bypass_budget(self):
        acc = CaptureAccumulator(budget=1000)
        acc.reserve_request_snapshot(b"m" * 800)
        for _ in range(500):
            acc.tee(b"chunk")   # 500×5=2500 字节涌入
        self.assertLessEqual(len(acc.captured()), 1000)

    def test_utf8_safe_truncation(self):
        acc = CaptureAccumulator(budget=7)
        acc.tee("中文中文".encode("utf-8"))  # 每字 3 字节；7 预算切 2 字留 6
        text = acc.captured().decode("utf-8", "replace")
        self.assertNotIn("�", text, "预算切点不产生半个字符")

    def test_exception_state_never_affects_passthrough(self):
        acc = CaptureAccumulator(budget=1)
        for i in range(10):
            self.assertEqual(acc.tee(b"abc"), b"abc")

    def test_captured_bytes_counts_budgeted_not_total(self):
        acc = CaptureAccumulator(budget=10)
        acc.tee(b"a" * 50)
        self.assertEqual(acc.total_seen, 50)
        self.assertEqual(len(acc.captured()), 10)


class TestStoreConvergence(unittest.TestCase):
    """issue #11：单条超大拒收 + append 后总量收敛。"""

    def test_oversized_record_rejected(self):
        import json as _json
        from capture import capture_store as cs
        import tempfile, pathlib, os
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as d:
            cs.append_json({"seed": 1}, os.path.join(d, "cap"))  # 首条建目录+marker
            d = os.path.join(d, "cap")
            with self.assertRaises(OSError):
                cs.append_json({"pad": "x" * (cs.MAX_RECORD_BYTES + 1)}, d)

    def test_post_append_trims_to_store_budget(self):
        from capture import capture_store as cs
        import tempfile, os
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as d:
            cs.append_json({"seed": 1}, os.path.join(d, "cap"))  # 建目录
            d = os.path.join(d, "cap")
            small = cs.MAX_STORE_BYTES
            old_total = cs.MAX_STORE_BYTES
            try:
                # 两文件各 ~3/4 上限 → append 第二条后总量超限触发收敛
                cs.MAX_STORE_BYTES = 100
                cs.MAX_FILE_BYTES = 1000
                cs.append_json({"d": "y" * 60}, d)
                # 人造第二个更旧文件使总量超限
                import time
                p = pathlib.Path(d) / "2026-01-01.jsonl"
                p.write_text("z" * 80)
                st = os.stat(p); os.utime(p, (st.st_atime - 100, st.st_mtime - 100))
                cs.append_json({"d": "y" * 5}, d)
                total = sum(f.stat().st_size for f in pathlib.Path(d).glob("*.jsonl*"))
                self.assertLessEqual(total, cs.MAX_STORE_BYTES + 20,
                                     "append 后总量收敛（旧文件被 trim）")
            finally:
                cs.MAX_STORE_BYTES = old_total
