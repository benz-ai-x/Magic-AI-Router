"""Tests for menu_builder.MenuBuilder — struct-key driven rebuild logic."""
import unittest
from unittest.mock import MagicMock

from shellui.menu_builder import MenuBuilder, MenuState


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
            forward_states=(("t-2", "AWS-ap", "connected"),))).struct_key()
        self.assertNotEqual(key_idle, key_fwd)

    def _tunnel_submenus(self, forward_states, cfg=None):
        app = MagicMock()
        # make_switch_tunnel/toggle_forward_session/make_reconnect_tunnel
        # 都返回可调用（rumps callback 形状）
        app.make_switch_tunnel.return_value = lambda _s: None
        app.toggle_forward_session.return_value = lambda _s: None
        app.make_reconnect_tunnel.return_value = lambda _s: None
        app.reconnect = lambda _s: None
        with unittest.mock.patch("shellui.menu_builder.chromium_proxy.installed_apps",
                                 return_value=[]):
            mb = MenuBuilder(app, lambda: _state(
                ssh_status="connected", config=cfg or self._cfg(),
                forward_states=forward_states))
            # 直接构建隧道子菜单（返回真实 rumps.MenuItem 树——MockApp 的
            # menu 不会真建树）
            parent = mb._build_tunnel_submenu()
        return [item for item in list(parent.values()) if hasattr(item, "values")]

    def test_per_tunnel_submenu_structure(self):
        subs = self._tunnel_submenus(
            (("t-2", "AWS-ap", "connected"),))
        titles = [s.title for s in subs]
        self.assertIn("Aws-eu — 已连接・代理", titles)
        self.assertIn("AWS-ap — 转发中", titles)
        by_name = {s.title.split(" — ")[0]: list(s.values()) for s in subs}
        proxy_items = [i.title for i in by_name["Aws-eu"]]
        fwd_items = [i.title for i in by_name["AWS-ap"]]
        self.assertIn("✓ 设为代理隧道", proxy_items)
        self.assertIn("重新连接", proxy_items)
        self.assertIn("设为代理隧道", fwd_items)
        self.assertIn("停止端口转发", fwd_items)
        self.assertIn("重新连接", fwd_items)

    def test_idle_tunnel_offers_start_with_hint_when_no_forwards(self):
        cfg = self._cfg()
        cfg["tunnels"][1]["forwards"] = []
        subs = self._tunnel_submenus((), cfg=cfg)
        by_name = {s.title.split(" — ")[0]: list(s.values()) for s in subs}
        fwd_items = [i.title for i in by_name["AWS-ap"]]
        self.assertIn("启动端口转发（需先配置转发规则）", fwd_items)

    def test_status_line_appends_forward_count(self):
        mb = MenuBuilder(MagicMock(), lambda: _state(
            ssh_status="connected", config=self._cfg(),
            forward_states=(("t-2", "AWS-ap", "connected"),
                            ("t-3", "x", "connecting"))))
        mb.build()
        mb.refresh_titles()
        title = mb.refs["proxy_status"].title
        self.assertIn("1 条转发", title)
