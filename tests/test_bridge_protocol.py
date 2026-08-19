"""Tests for bridge_protocol.py — 设置窗桥接协议核心。

Seam under test: BridgeCore — dict-in/dict-out, no PyObjC.

Regression anchors: the four historical crash classes of the ObjC bridge —
1. Python hand-interpolating JS source (path with newline → broken JS)
2. handler exceptions traversing into ObjC (must never propagate)
3. Python naming JS DOM selectors (field drift → safe no-op)
4. stale dirty mirror (close-guard must track the latest dirtyState)
"""
import json
import re
import unittest

from bridge_protocol import BridgeCore


def extract_json(js):
    """Pull the JSON argument out of window.__native.receive(<json>)."""
    m = re.fullmatch(
        r"window\.__native&&window\.__native\.receive\((.*)\)", js, re.DOTALL)
    assert m, f"unexpected JS shape: {js!r}"
    return json.loads(m.group(1))


class TestBuildFillJs(unittest.TestCase):
    """Crash class 1: no hand-interpolated JS — json.dumps is the only escaping."""

    def test_plain_path_round_trips(self):
        msg = extract_json(BridgeCore.build_fill_js("sshKey", "/Users/x/.ssh/id_rsa"))
        self.assertEqual(msg["type"], "keyFilePicked")
        self.assertEqual(msg["payload"], {"field": "sshKey", "path": "/Users/x/.ssh/id_rsa"})

    def test_nasty_path_round_trips_unchanged(self):
        nasty = ("/Users/we írd/.ssh/k'y \"dq\" \\back\n"
                 "newline\ttab\u2028linesep 制表符.pem")
        msg = extract_json(BridgeCore.build_fill_js("sshKey", nasty))
        self.assertEqual(msg["payload"]["path"], nasty)

    def test_empty_path_still_valid(self):
        msg = extract_json(BridgeCore.build_fill_js("sshKey", ""))
        self.assertEqual(msg["payload"]["path"], "")


class _HostileMapping:
    """A mapping whose every accessor raises — must be neutralized."""

    def keys(self):
        raise RuntimeError("boom")

    def __getitem__(self, k):
        raise RuntimeError("boom")


class _ObjCBridgedDict:
    """Simulates a PyObjC-bridged NSDictionary: a mapping, but NOT a dict."""

    def __init__(self, data):
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, k):
        return self._data[k]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class TestObjCBridgedContainers(unittest.TestCase):
    """Regression: real bridge bodies arrive as NSDictionary — the nested
    payload failed isinstance(dict) and pickKeyFile was dropped (field=None)."""

    def test_nested_bridged_payload_dispatches(self):
        core = BridgeCore()
        body = _ObjCBridgedDict({
            "type": "pickKeyFile",
            "payload": _ObjCBridgedDict({"field": "sshKey"}),
        })
        self.assertEqual(core.handle_message(body),
                         [{"type": "showOpenPanel", "field": "sshKey"}])

    def test_nested_bridged_dirty_state(self):
        core = BridgeCore()
        core.handle_message(_ObjCBridgedDict({
            "type": "dirtyState",
            "payload": _ObjCBridgedDict({"dirty": True}),
        }))
        self.assertTrue(core.dirty)


class TestHandleMessageRobustness(unittest.TestCase):
    """Crash class 2: nothing the JS/ObjC side sends may crash the bridge."""

    def test_handler_exception_returns_empty_and_never_raises(self):
        core = BridgeCore()
        with self.assertLogs("magic-proxy.bridge", level="WARNING"):
            actions = core.handle_message(
                _HostileMapping())
        self.assertEqual(actions, [])
        self.assertFalse(core.dirty)

    def test_non_dict_message_ignored(self):
        core = BridgeCore()
        self.assertEqual(core.handle_message("dirtyState"), [])
        self.assertEqual(core.handle_message(None), [])
        self.assertEqual(core.handle_message(["pickKeyFile"]), [])

    def test_missing_payload_treated_as_empty(self):
        core = BridgeCore()
        self.assertEqual(core.handle_message({"type": "pickKeyFile"}), [])

    def test_scalar_payload_does_not_raise(self):
        # Legacy JS posted a raw bool as the whole message body.
        core = BridgeCore()
        self.assertEqual(core.handle_message({"type": "dirtyState", "payload": True}), [])
        self.assertFalse(core.dirty)


class TestPickKeyFileDispatch(unittest.TestCase):
    """Crash class 3: field names are a closed set owned by the core."""

    def test_known_field_returns_open_panel_action(self):
        core = BridgeCore()
        actions = core.handle_message(
            {"type": "pickKeyFile", "payload": {"field": "sshKey"}})
        self.assertEqual(actions, [{"type": "showOpenPanel", "field": "sshKey"}])

    def test_unknown_field_is_safe_noop(self):
        core = BridgeCore()
        self.assertEqual(
            core.handle_message({"type": "pickKeyFile", "payload": {"field": "bogus"}}),
            [])

    def test_unknown_message_type_is_safe_noop(self):
        core = BridgeCore()
        self.assertEqual(core.handle_message({"type": "nope", "payload": {}}), [])


class TestAppActionsDispatch(unittest.TestCase):
    """reconnectProxy / openPath — app-level actions with closed kind sets."""

    def test_reconnect_returns_action(self):
        core = BridgeCore()
        self.assertEqual(core.handle_message({"type": "reconnectProxy", "payload": {}}),
                         [{"type": "reconnectProxy"}])

    def test_reconnect_ignores_payload_content(self):
        core = BridgeCore()
        self.assertEqual(
            core.handle_message({"type": "reconnectProxy", "payload": {"spam": 1}}),
            [{"type": "reconnectProxy"}])

    def test_open_path_known_kind_returns_action(self):
        core = BridgeCore()
        self.assertEqual(
            core.handle_message({"type": "openPath", "payload": {"kind": "captureDir"}}),
            [{"type": "openPath", "kind": "captureDir"}])

    def test_open_path_unknown_kind_is_safe_noop(self):
        core = BridgeCore()
        with self.assertLogs("magic-proxy.bridge", level="WARNING"):
            self.assertEqual(
                core.handle_message({"type": "openPath", "payload": {"kind": "/etc"}}),
                [])

    def test_open_path_bridged_payload_dispatches(self):
        core = BridgeCore()
        actions = core.handle_message(_ObjCBridgedDict({
            "type": "openPath",
            "payload": _ObjCBridgedDict({"kind": "captureDir"}),
        }))
        self.assertEqual(actions, [{"type": "openPath", "kind": "captureDir"}])


class TestDirtyStateMachine(unittest.TestCase):
    """Crash class 4: the close-guard mirror tracks the latest message."""

    def test_starts_clean(self):
        self.assertFalse(BridgeCore().dirty)

    def test_dirty_then_clean(self):
        core = BridgeCore()
        self.assertEqual(
            core.handle_message({"type": "dirtyState", "payload": {"dirty": True}}), [])
        self.assertTrue(core.dirty)
        core.handle_message({"type": "dirtyState", "payload": {"dirty": False}})
        self.assertFalse(core.dirty)

    def test_latest_message_wins_after_rapid_sequence(self):
        core = BridgeCore()
        for _ in range(5):
            core.handle_message({"type": "dirtyState", "payload": {"dirty": True}})
            core.handle_message({"type": "dirtyState", "payload": {"dirty": False}})
            core.handle_message({"type": "dirtyState", "payload": {"dirty": True}})
        self.assertTrue(core.dirty)

    def test_truthy_non_bool_coerced(self):
        core = BridgeCore()
        core.handle_message({"type": "dirtyState", "payload": {"dirty": 1}})
        self.assertTrue(core.dirty)
        core.handle_message({"type": "dirtyState", "payload": {"dirty": ""}})
        self.assertFalse(core.dirty)


if __name__ == "__main__":
    unittest.main()
