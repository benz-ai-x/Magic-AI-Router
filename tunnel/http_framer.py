"""明文 HTTP 逐消息增量 framer（issue #5）.

每条消息（请求/响应）在转发前完成解析与定界：起始行 + 头部 + 按
Content-Length / chunked 定界的 body。StreamReader 的自然缓冲使
pipelined 的后续消息字节自动留给下一轮（readexactly 只取本消息所需）。

安全拒绝策略：无法定界的消息按「本消息后关闭连接」处理——绝不带着
未定界状态继续复用连接。
"""
from __future__ import annotations

CHUNK = 65536


async def read_head(reader, first_line=None, max_bytes=16384):
    """读一条消息的起始行 + 头部行。

    返回 (start_line_bytes, [header_line_bytes...])；在起始行前遇到 EOF
    返回 None（对端正常收尾）。头部超限抛 ValueError。
    """
    line = first_line if first_line is not None else await reader.readline()
    if not line:
        return None
    start = line
    total = len(line)
    headers = []
    while True:
        line = await reader.readline()
        total += len(line)
        if total > max_bytes:
            raise ValueError("HTTP header exceeds limit")
        if line in (b"\r\n", b"\n", b""):
            return start, headers
        headers.append(line)


def parse_framing(header_lines):
    """从头部分析 body 定界：返回 (content_length, chunked, conn_close)。"""
    content_length = None
    chunked = False
    conn_close = False
    for raw in header_lines:
        name, sep, value = raw.decode("iso-8859-1", "replace").partition(":")
        if not sep:
            continue
        n = name.strip().lower()
        v = value.strip()
        if n == "content-length" and content_length is None:
            content_length = int(v)  # 非法值 → ValueError → 调用方安全关闭
        elif n == "transfer-encoding" and "chunked" in v.lower():
            chunked = True
        elif n == "connection" and "close" in v.lower():
            conn_close = True
    return content_length, chunked, conn_close


async def relay_fixed(src, dst, length):
    """按 Content-Length 精确转发 length 字节。截断抛 ConnectionError。"""
    remaining = length
    while remaining > 0:
        chunk = await src.read(min(CHUNK, remaining))
        if not chunk:
            raise ConnectionError("body truncated before Content-Length satisfied")
        dst.write(chunk)
        remaining -= len(chunk)


async def relay_chunked(src, dst):
    """逐行转发 chunked 编码至终止块。返回 False = 流中断/格式异常。"""
    while True:
        line = await src.readline()
        if not line:
            return False
        dst.write(line)
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return False
        if size == 0:
            while True:  # 终止块后的 trailer 行至空行
                t = await src.readline()
                if not t:
                    return False
                dst.write(t)
                if t in (b"\r\n", b"\n"):
                    return True
        remaining = size
        while remaining > 0:
            chunk = await src.read(min(CHUNK, remaining))
            if not chunk:
                return False
            dst.write(chunk)
            remaining -= len(chunk)
        crlf = await src.readline()  # 数据块尾 CRLF
        if not crlf:
            return False
        dst.write(crlf)


async def relay_until_eof(src, dst):
    """无定界 body：转发至 EOF（连接语义随之终止）。"""
    while True:
        chunk = await src.read(CHUNK)
        if not chunk:
            return
        dst.write(chunk)
