"""tunnel/http_framer.py 直测（issue #5）：定界契约的单元级钉死.

含分片到达（多次 feed + 让出事件循环）——StreamReader 阻塞读在字节
分段抵达时必须正确等待与拼接。
"""
import asyncio
import unittest

from tunnel import http_framer as hf


def _stream(*chunks):
    r = asyncio.StreamReader()
    for c in chunks:
        r.feed_data(c)
    r.feed_eof()
    return r


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, d):
        self.data += d


class TestReadHead(unittest.IsolatedAsyncioTestCase):
    async def test_basic_head(self):
        head = await hf.read_head(_stream(b"GET / HTTP/1.1\r\nHost: x\r\n\r\nbody"))
        self.assertEqual(head[0], b"GET / HTTP/1.1\r\n")
        self.assertEqual(head[1], [b"Host: x\r\n"])

    async def test_split_arrival_waits_and_assembles(self):
        """分片到达：头部行跨多次 feed 抵达仍正确拼接。"""
        r = asyncio.StreamReader()
        r.feed_data(b"GET / HTTP/1.1\r\nHo")
        task = asyncio.ensure_future(hf.read_head(r))
        await asyncio.sleep(0)
        r.feed_data(b"st: x")
        await asyncio.sleep(0)
        r.feed_data(b"\r\n\r\nGET-NEXT")
        r.feed_eof()
        start, headers = await task
        self.assertEqual(start, b"GET / HTTP/1.1\r\n")
        self.assertEqual(headers, [b"Host: x\r\n"])

    async def test_eof_before_start_returns_none(self):
        self.assertIsNone(await hf.read_head(_stream(b"")))

    async def test_mid_header_eof_raises(self):
        """头部中途 EOF：残缺请求安全拒绝，绝不静默补全。"""
        r = _stream(b"GET / HTTP/1.1\r\nHost: x\r\n")  # 无终结空行即 EOF
        with self.assertRaises(ConnectionError):
            await hf.read_head(r)

    async def test_header_limit_raises(self):
        big = b"X-Pad: " + b"a" * 2000 + b"\r\n\r\n"
        with self.assertRaises(ValueError):
            await hf.read_head(_stream(b"GET / HTTP/1.1\r\n", big),
                               max_bytes=1024)


class TestParseFraming(unittest.TestCase):
    def test_content_length(self):
        f = hf.parse_framing([b"Content-Length: 42\r\n"])
        self.assertEqual((f.content_length, f.chunked, f.conn_close), (42, False, False))

    def test_chunked_and_close(self):
        f = hf.parse_framing([b"Transfer-Encoding: chunked\r\n",
                              b"Connection: close\r\n"])
        self.assertTrue(f.chunked and f.conn_close)

    def test_illegal_content_length_raises(self):
        with self.assertRaises(ValueError):
            hf.parse_framing([b"Content-Length: abc\r\n"])

    def test_split_header(self):
        self.assertEqual(hf.split_header(b"Host: x.y\r\n"), ("host", "x.y"))
        self.assertEqual(hf.split_header(b"NoColon\r\n"), (None, ""))


class TestBodyRelay(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_exact_bytes(self):
        sink = _Sink()
        await hf.relay_fixed(_stream(b"abcdef"), sink, 6)
        self.assertEqual(sink.data, b"abcdef")

    async def test_fixed_truncated_raises(self):
        with self.assertRaises(ConnectionError):
            await hf.relay_fixed(_stream(b"abc"), _Sink(), 6)

    async def test_chunked_with_trailer(self):
        sink = _Sink()
        ok = await hf.relay_chunked(
            _stream(b"5\r\nhello\r\n0\r\nX-T: v\r\n\r\nNEXT"), sink)
        self.assertTrue(ok)
        self.assertEqual(sink.data, b"5\r\nhello\r\n0\r\nX-T: v\r\n\r\n")

    async def test_chunked_blank_size_line_rejected(self):
        ok = await hf.relay_chunked(_stream(b"\r\n5\r\nhello\r\n"), _Sink())
        self.assertFalse(ok, "空白 size 行必须拒绝而非充当终止块")

    async def test_until_eof(self):
        sink = _Sink()
        await hf.relay_until_eof(_stream(b"xyz"), sink)
        self.assertEqual(sink.data, b"xyz")


if __name__ == "__main__":
    unittest.main()
