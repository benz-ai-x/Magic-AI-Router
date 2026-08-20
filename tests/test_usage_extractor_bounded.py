"""UsageExtractor 有界线性增量解析（issue #13）.

验收锚点：每输入字节摊销 O(1)（bytearray/cursor，已消费前缀及时释放）；
单 event / json_mode 文档有明确上限；超限进入 terminal truncated 态。
"""
import time
import unittest

from suanpan.usage_extractor import UsageExtractor


class TestLinearAmortized(unittest.TestCase):
    def test_1byte_chunks_stay_linear(self):
        """1 字节 chunk × 50K 输入：完成时间不得平方爆炸。"""
        ext = UsageExtractor()
        payload = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n'
        chunk = payload * 50000  # ~100KB 输入
        t0 = time.monotonic()
        for b in chunk:
            ext.feed(bytes([b]))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0,
                        f"1-byte chunk ×50K 耗时 {elapsed:.2f}s——平方复杂度泄漏")

    def test_consumed_prefix_freed(self):
        """处理过的前缀不应留在缓冲里。"""
        ext = UsageExtractor()
        big = b'data: {"type":"message_start","message":{"usage":{"input_tokens":100}}}\n\n' * 100
        ext.feed(big)
        self.assertLess(ext._buf_bytes(), len(big) // 2,
                        "已消费前缀应及时释放")

    def test_unterminated_event_capped(self):
        """无终止 event 不得超过单 event 上限。"""
        ext = UsageExtractor()
        monster = b"x" * (5 * 1024 * 1024 + 1)  # > 单 event 上限
        ext.feed(monster)
        self.assertTrue(ext.truncated)
        # terminal 态：后续输入被丢弃
        ext.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n')
        self.assertEqual(ext.input_tokens, 0)


class TestJsonModeBounded(unittest.TestCase):
    def test_json_body_capped_and_terminal(self):
        ext = UsageExtractor(json_mode=True)
        monster = b"{" + b"x" * (8 * 1024 * 1024)
        ext.feed(monster)
        self.assertTrue(ext.truncated)
        # 超限后终端态：不再解析后续
        self.assertEqual(ext.input_tokens, 0)

    def test_normal_json_body_still_parses(self):
        ext = UsageExtractor(json_mode=True)
        ext.feed(b'{"usage":{"input_tokens":42,"output_tokens":7}}')
        self.assertEqual(ext.input_tokens, 42)
        self.assertFalse(ext.truncated)


class TestCompat(unittest.TestCase):
    """CRLF / 裸 CR / data: 与 data: / 非法 JSON 行兼容不变。"""

    def test_crlf_and_bare_cr(self):
        ext = UsageExtractor()
        ext.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\r\n\r\n')
        ext.feed(b'data: {"type":"message_delta","usage":{"output_tokens":5}}\r\r')
        self.assertEqual(ext.input_tokens, 10)
        self.assertEqual(ext.output_tokens, 5)

    def test_data_variants(self):
        ext = UsageExtractor()
        ext.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n\n')
        ext.feed(b'data:{"type":"message_delta","usage":{"output_tokens":2}}\n\n')
        self.assertEqual(ext.input_tokens, 1)
        self.assertEqual(ext.output_tokens, 2)


if __name__ == "__main__":
    unittest.main()
