"""AuthenticatedHttpClient（issue #4）：认证请求的统一出站 adapter.

三个余额/模型/连通用调用方（fetch_models / test_provider /
fetch_balance）只传请求意图；redirect、scheme、超时、响应上限策略集中
在此：

- 跨 origin（scheme / host / effective port 任一变化）重定向**一律拒绝**
  ——Authorization、x-api-key 及未来任何自定义敏感头共用同一策略：
  认证请求绝不出原始 origin。
- HTTPS → HTTP 降级必拒。
- 同 origin 重定向放行（凭证保留），最大跳数 3，允许 301/302/303/307/308。
- 响应体上限 1MB，防失控放大。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger("magic-proxy.auth_http")

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 1024 * 1024
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_DEFAULT_PORTS = {"http": 80, "https": 443}


class AuthRedirectError(Exception):
    """认证请求的重定向被策略拒绝；msg 可直接展示。"""

    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


def _origin_of(url: str):
    parts = urllib.parse.urlsplit(url)
    port = parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


class _AuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """同 origin 显式放行、跨 origin/降级一律拒绝的重定向策略。"""

    def __init__(self, origin_url: str):
        self._origin = _origin_of(origin_url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code not in _REDIRECT_STATUSES:
            return None
        old = _origin_of(req.full_url)
        new = _origin_of(newurl)
        if new[0] == "http" and old[0] == "https":
            raise AuthRedirectError(
                f"拒绝 HTTPS→HTTP 降级重定向：{req.full_url} → {newurl}")
        if new != old:
            raise AuthRedirectError(
                f"拒绝跨 origin 重定向（凭证不得离开原始 origin）："
                f"{req.full_url} → {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AuthenticatedHttpClient:
    """认证请求的统一出站口。策略见模块 docstring。"""

    def __init__(self, timeout=10, max_bytes=MAX_RESPONSE_BYTES,
                 max_redirects=MAX_REDIRECTS):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    def open(self, url, headers=None, data=None, method=None,
             timeout=None) -> bytes:
        """发认证请求返回 body 字节。策略性失败抛 AuthRedirectError。"""
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method=method)
        handler = _AuthRedirectHandler(url)
        handler.max_redirections = self.max_redirects
        opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=timeout or self.timeout) as resp:
            body = resp.read(self.max_bytes + 1)
        if len(body) > self.max_bytes:
            raise ValueError(f"响应超过 {self.max_bytes} 字节上限")
        return body

    def open_json(self, url, headers=None, data=None, method=None,
                  timeout=None):
        return json.loads(self.open(url, headers=headers, data=data,
                                    method=method, timeout=timeout))
