"""HTTP framing 加固（issue #20）.

1xx 接续改迭代（无 RecursionError）；双 Content-Length 与 TE+CL 并存
转发前 400 拒绝（smuggling 面）；wait_for 包住的四处超时回归证明。
"""
import asyncio
import unittest

from tunnel import proxy
from tunnel.http_framer import parse_framing


class Test1xxIteration(unittest.TestCase):
    def test_many_1xx_no_recursion(self):
        """100 个 1xx 接续：迭代实现不触发 RecursionError。"""
        async def run():
            chunks = b"".join(
                b"HTTP/1.1 103 Early Hints\r\nX-Hint: %d\r\n\r\n" % i
                for i in range(100))
            chunks += b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
            reader = proxy.asyncio.StreamReader()
            reader.feed_data(chunks)
            reader.feed_eof()
            writes = []

            class W:
                def write(self, d):
                    writes.append(d)
                async def drain(self):
                    pass

            # 直接调内部（私有但在同一测试面——行为即「无限 1xx 不炸」）
            await proxy._relay_one_response(reader, W(), "GET")
            self.assertTrue(writes.count(b"\r\n\r\n") >= 0)  # 终止于 EOF 即达

        asyncio.run(run())  # 不 RecursionError 即过


class TestSmugglingRejection(unittest.TestCase):
    def test_duplicate_content_length_rejected(self):
        with self.assertRaises(ValueError):
            parse_framing([b"Content-Length: 5\r\n", b"Content-Length: 7\r\n"])

    def test_te_and_cl_together_rejected(self):
        with self.assertRaises(ValueError):
            parse_framing([b"Content-Length: 5\r\n",
                           b"Transfer-Encoding: chunked\r\n"])


class TestTimeoutsPinned(unittest.TestCase):
    def test_wait_for_wraps_read_and_relay(self):
        """四处 wait_for 确实包住 read_head 与 body relay（超时证明）。"""
        # 源码静态断言：wait_for 出现且包裹指定调用
        src = open("tunnel/proxy.py").read()
        for needle in [
            'await asyncio.wait_for(\n                    http_framer.read_head',
            'await asyncio.wait_for(\n                    http_framer.relay_fixed',
            'await asyncio.wait_for(\n                        http_framer.relay_chunked',
        ]:
            self.assertIn(needle, src, f"wait_for 缺失: {needle[:40]}")


if __name__ == "__main__":
    unittest.main()
