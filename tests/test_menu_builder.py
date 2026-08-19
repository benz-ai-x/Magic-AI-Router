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
