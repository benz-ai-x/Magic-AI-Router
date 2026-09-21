"""Tests for menu_builder.MenuBuilder — struct-key driven rebuild logic."""
import unittest
from unittest.mock import MagicMock

import rumps

from shellui.menu_builder import MenuBuilder, MenuState, _proxy_tunnel_index
from tunnel.connection_coordinator import ForwardState
from mount.coordinator import MountState


def _state(**overrides):
    base = dict(
        ssh_status="stopped", ssh_cmd_str="", ssh_log="", ssh_error_msg="",
        paused=False,
        stats_snapshot={"active_connections": 0, "rate_down": 0.0, "rate_up": 0.0},
        config={}, sys_proxy_on=False, sys_proxy_error="",
        capture_menu_title="开始抓包", capture_error_hint=None,
        suanpan_running=False, suanpan_error="", suanpan_listen_address="",
        current_tunnel=None, prevent_sleep_title="阻止睡眠",
        launch_login_title="开机启动",
    )
    base.update(overrides)
    return MenuState(**base)


class TestStructKey(unittest.TestCase):
    def test_connection_count_does_not_rebuild_menu(self):
        # #40: active_connections fluctuates every tick while traffic flows;
        # it only affects the traffic *title*, never the menu structure.
        key_idle = MenuBuilder(MagicMock(), lambda: _state()).struct_key()
        key_busy = MenuBuilder(
            MagicMock(),
            lambda: _state(stats_snapshot={
                "active_connections": 5, "rate_down": 1.0, "rate_up": 1.0}),
        ).struct_key()
        self.assertEqual(key_idle, key_busy)


if __name__ == "__main__":
    unittest.main()


class TestMultiActiveTunnels(unittest.TestCase):
    """多活（v0.9）：forward_states 参与 struct_key + 子菜单构建。"""

    @staticmethod
    def _cfg():
        return {"current_tunnel": 0, "tunnels": [
            {"id": "t-1", "name": "Aws-eu", "ssh_host": "a",
             "forwards": [{"local_port": 9000, "remote_host": "h",
                           "remote_port": 80}]},
            {"id": "t-2", "name": "AWS-ap", "ssh_host": "b",
             "forwards": [{"local_port": 9001, "remote_host": "h",
                           "remote_port": 81}]},
        ]}

    def test_forward_state_change_rebuilds_menu(self):
        key_idle = MenuBuilder(MagicMock(), lambda: _state(
            config=self._cfg(), forward_states=())).struct_key()
        key_fwd = MenuBuilder(MagicMock(), lambda: _state(
            config=self._cfg(),
            forward_states=(ForwardState("t-2", "AWS-ap", "connected"),))).struct_key()
        self.assertNotEqual(key_idle, key_fwd)

    def _submenu(self, title, cfg=None, forward_states=(), ssh_status="connected"):
        """直接构建子菜单（真实 rumps.MenuItem 树——MockApp 的 menu 不会
        真建树）；返回 (parent, [子行 MenuItem])。"""
        app = MagicMock()
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[{"name": "ChatGPT"}]):
            mb = MenuBuilder(app, lambda: _state(
                ssh_status=ssh_status, config=cfg or self._cfg(),
                forward_states=forward_states))
            builder = {"代 理": mb._build_proxy_submenu,
                       "端口映射": mb._build_forward_submenu,
                       "系 统": mb._build_system_submenu}[title]
            parent = builder()
        rows = list(parent.values())
        self._titles = [r.title for r in rows if hasattr(r, "title")]
        return parent, [r for r in rows if hasattr(r, "values")]

    def test_proxy_submenu_structure(self):
        parent, subs = self._submenu("代 理")
        titles = self._titles
        self.assertIn("暂停代理", titles)          # connected 语境
        self.assertIn("重新连接", titles)
        self.assertIn("系统代理：关", titles)
        self.assertIn("代理隧道（SOCKS5 上游）", titles)
        self.assertIn("✓ Aws-eu", titles)          # 角色单选：当前打 ✓
        self.assertIn("AWS-ap", titles)
        launch = [t for t in titles if t == "经代理启动 App"]
        self.assertEqual(len(launch), 1)
        launch_rows = [s for s in subs if s.title == "经代理启动 App"]
        self.assertIn("ChatGPT", [i.title for i in list(launch_rows[0].values())])

    def test_forward_submenu_structure(self):
        parent, subs = self._submenu(
            "端口映射", forward_states=(ForwardState("t-2", "AWS-ap", "connected"),))
        titles = self._titles
        self.assertIn("Aws-eu — 随代理运行", titles)   # 代理隧道信息行
        running = [s for s in subs if s.title.startswith("AWS-ap")]
        self.assertTrue(running, titles)
        self.assertIn("— 转发中", running[0].title)
        self.assertIn("9001→81", running[0].title)     # 转发摘要
        items = [i.title for i in list(running[0].values())]
        self.assertIn("停止端口转发", items)
        self.assertIn("重新连接", items)

    def test_forward_submenu_idle_and_no_rules(self):
        cfg = self._cfg()
        cfg["tunnels"][1]["forwards"] = []
        parent, subs = self._submenu("端口映射", cfg=cfg)
        titles = self._titles
        idle = [s for s in subs if s.title.startswith("AWS-ap")]
        self.assertIn("— 未启动", idle[0].title)
        items = [i.title for i in list(idle[0].values())]
        self.assertIn("启动端口转发（需先配置转发规则）", items)
        # t-1 有规则 → 不出现全局空态提示
        self.assertNotIn("在偏好设置 → 隧道里添加转发规则", titles)

    def test_forward_submenu_empty_state_hint(self):
        cfg = self._cfg()
        for t in cfg["tunnels"]:
            t["forwards"] = []
        parent, _ = self._submenu("端口映射", cfg=cfg)
        titles = self._titles
        self.assertIn("在偏好设置 → 隧道里添加转发规则", titles)

    def test_forward_submenu_proxy_row_reflects_disconnected(self):
        parent, _ = self._submenu("端口映射", ssh_status="stopped")
        titles = self._titles
        self.assertIn("Aws-eu — 未随代理运行", titles)

    def test_system_submenu_and_refs(self):
        _, _subs = self._submenu("系 统")
        titles = self._titles
        self.assertIn("阻止睡眠", titles)   # fixture 文案（=防睡眠开关）
        self.assertIn("开机启动", titles)   # fixture 文案（=登录启动开关）
        # ADR-009：配置 API 服务开关（MenuState 字段缺省 = 关）
        self.assertIn("配置 API 服务：关", titles)

    def test_status_line_appends_forward_count(self):
        mb = MenuBuilder(MagicMock(), lambda: _state(
            ssh_status="connected", config=self._cfg(),
            forward_states=(ForwardState("t-2", "AWS-ap", "connected"),
                            ForwardState("t-3", "x", "connecting"))))
        mb.build()
        mb.refresh_titles()
        title = mb.refs["proxy_status"].title
        self.assertIn("1 条转发", title)
        self.assertNotIn("🟢", title, "状态行 emoji 已退役（颜色由图标承载）")


class TestIconInfrastructure(unittest.TestCase):
    """SF Symbols 图标基建：正常返回图像，异常静默降级不抛。"""

    def test_unknown_symbol_returns_none(self):
        from shellui import menu_builder
        self.assertIsNone(menu_builder._symbol_image("definitely-not-a-symbol"))

    def test_apply_icon_tolerates_none_item_and_bad_key(self):
        from shellui import menu_builder
        menu_builder._apply_icon(None, "proxy_menu")  # 不抛即过
        menu_builder._apply_icon(rumps.MenuItem("x", callback=None),
                                 "no-such-key")

    def test_status_color_kinds(self):
        from shellui import menu_builder
        for kind in ("ok", "warn", "err", "idle"):
            self.assertIsNotNone(menu_builder._status_color(kind))


class TestProxyTunnelIndex(unittest.TestCase):
    """角色解析序（v0.9.2）：id 真相 → 旧下标 → 首条（与 merge 同语义，
    菜单只消费不重定义）。"""

    def test_id_wins_over_index(self):
        cfg = {"current_tunnel": 0, "current_tunnel_id": "t-b",
               "tunnels": [{"id": "t-a"}, {"id": "t-b"}]}
        self.assertEqual(_proxy_tunnel_index(cfg), 1)

    def test_dangling_id_falls_back_to_index(self):
        cfg = {"current_tunnel": 1, "current_tunnel_id": "gone",
               "tunnels": [{"id": "t-a"}, {"id": "t-b"}]}
        self.assertEqual(_proxy_tunnel_index(cfg), 1)

    def test_index_out_of_range_resets_to_first(self):
        cfg = {"current_tunnel": 5, "tunnels": [{"id": "t-a"}]}
        self.assertEqual(_proxy_tunnel_index(cfg), 0)

    def test_malformed_config_is_safe(self):
        self.assertEqual(_proxy_tunnel_index(None), 0)
        self.assertEqual(_proxy_tunnel_index({}), 0)


class TestMountSubmenu(unittest.TestCase):
    """远程挂载（ADR-007）：mount_states 参与 struct_key + 子菜单构建。"""

    @staticmethod
    def _mounts():
        return (MountState("t-1", "Aws-eu", "data", "mounted", ""),
                MountState("t-1", "Aws-eu", "ws", "unmounted", ""))

    def _submenu(self, mount_states=(), cfg=None):
        app = MagicMock()
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[]):
            mb = MenuBuilder(app, lambda: _state(
                config=cfg or {"tunnels": []}, mount_states=mount_states))
            parent = mb._build_mount_submenu()
        rows = [r for r in parent.values() if hasattr(r, "values")]
        titles = [r.title for r in parent.values() if hasattr(r, "title")]
        return parent, rows, titles

    def test_mount_state_change_rebuilds_menu(self):
        key_idle = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=())).struct_key()
        key_mounted = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=self._mounts())).struct_key()
        self.assertNotEqual(key_idle, key_mounted)
        # 状态迁移（unmounted→mounting）同样触发重建
        key_shift = MenuBuilder(MagicMock(), lambda: _state(
            mount_states=(MountState("t-1", "Aws-eu", "ws", "mounting", ""),))).struct_key()
        self.assertNotEqual(key_mounted, key_shift)

    def test_mount_rows_and_actions(self):
        _, rows, titles = self._submenu(self._mounts())
        mounted = [r for r in rows if r.title.startswith("Aws-eu · data")]
        unmounted = [r for r in rows if r.title.startswith("Aws-eu · ws")]
        self.assertTrue(mounted and unmounted, titles)
        self.assertIn("— 已挂载", mounted[0].title)
        self.assertIn("— 未挂载", unmounted[0].title)
        items = [i.title for i in list(mounted[0].values())]
        self.assertIn("卸载", items)
        self.assertIn("打开挂载目录", items)
        items = [i.title for i in list(unmounted[0].values())]
        self.assertIn("挂载", items)

    def test_error_row_shows_message(self):
        states = (MountState("t-1", "srv", "data", "error", "挂载失败：权限不足"),)
        _, rows, titles = self._submenu(states)
        err = [r for r in rows if r.title.startswith("srv · data")]
        self.assertIn("— 异常", err[0].title)
        items = [i.title for i in list(err[0].values())]
        self.assertIn("  挂载失败：权限不足", items)

    def test_empty_state_hint(self):
        _, _, titles = self._submenu()
        self.assertIn("在偏好设置 → 远程挂载里配置", titles)

    def test_status_line_appends_mount_count(self):
        mb = MenuBuilder(MagicMock(), lambda: _state(
            ssh_status="connected", mount_states=self._mounts()))
        mb.build()
        mb.refresh_titles()
        title = mb.refs["proxy_status"].title
        self.assertIn("1 挂载", title)
