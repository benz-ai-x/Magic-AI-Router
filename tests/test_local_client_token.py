"""本地客户端 token 契约（issue #9，决策 A×4）.

- 每安装实例专用随机 token（无业务含义），存 `~/.magic-proxy.json` 字段
- 单活轮换：任意时刻一个有效值，轮换即刻作废旧值
- 网关 token 为空=不校验本地客户端，但出站**无条件**剥除一切入站
  Authorization/x-api-key（含 mage-router/本地 token/用户真实值）
- 永不回显明文于 UI/日志/diff（掩码布尔契约）
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mpconf.local_token import (get_local_token, rotate_token,
                                mask_token_state)
from mpconf.provider_auth import build_outbound_headers


class TestTokenLifecycle(unittest.TestCase):
    def test_generate_once_and_persist_0600(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            tok = get_local_token(cfg_path)
            self.assertTrue(tok)
            self.assertEqual(len(tok), 32, "token_hex(16) = 32 hex chars")
            # 落盘 0600 + 幂等
            self.assertEqual(stat.S_IMODE(os.stat(cfg_path).st_mode), 0o600)
            self.assertEqual(get_local_token(cfg_path), tok)

    def test_rotation_single_active_old_value_dead(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            old = get_local_token(cfg_path)
            new = rotate_token(cfg_path)
            self.assertNotEqual(old, new)
            self.assertEqual(get_local_token(cfg_path), new)
            # 旧值不存在于文件
            self.assertNotIn(old, Path(cfg_path).read_text())

    def test_mask_state_never_echoes_plaintext(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            tok = get_local_token(cfg_path)
            state = mask_token_state(cfg_path)
            self.assertTrue(state["token_set"])
            self.assertNotIn(tok, json.dumps(state))


class TestUnconditionalOutboundStripping(unittest.TestCase):
    """验收⑤：keyless Provider 出站绝不透传任何入站凭证。"""

    def test_keyless_provider_strips_all_incoming_auth(self):
        incoming = {"Authorization": "Bearer mage-router",
                    "x-api-key": "real-user-secret"}
        out = build_outbound_headers(incoming, api_key=None)
        self.assertNotIn("Authorization", out)
        self.assertNotIn("x-api-key", out)

    def test_keyless_provider_strips_local_token_too(self):
        incoming = {"Authorization": "Bearer lc-abc123"}
        out = build_outbound_headers(incoming, api_key=None,
                                     gateway_key="lc-abc123")
        self.assertNotIn("Authorization", out)


if __name__ == "__main__":
    unittest.main()


class TestPreviewMasking(unittest.TestCase):
    """验收⑥：preview diff 永不回显 token 明文（新旧两端都掩码）。"""

    def test_preview_never_echoes_local_token(self):
        import json as _json
        with tempfile.TemporaryDirectory() as d:
            cfg_path = str(Path(d) / "magic-proxy.json")
            settings_path = str(Path(d) / "settings.json")
            from mpconf.local_token import get_local_token
            from services.claude_code_setup import preview
            tok = get_local_token(cfg_path)
            with patch("services.claude_code_setup.config_store.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={}), \
                 patch("services.claude_code_setup.config_store.get_path",
                       side_effect=lambda k: cfg_path if k == "mp" else settings_path), \
                 patch("services.claude_code_setup.config_store.PATHS",
                       {"claude_settings": settings_path}):
                pv = preview()
            self.assertNotIn(tok, _json.dumps(pv),
                             "preview diff 不得回显本地 token 明文")
