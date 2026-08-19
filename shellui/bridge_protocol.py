"""设置窗桥接协议核心 — pure-Python half of the WKWebView bridge.

Owns the message protocol between config_ui.html (JS) and the native
settings window: message dispatch, the dirty-state machine backing the
window close-guard, and outbound JS construction. No PyObjC/AppKit
imports, so the whole protocol is testable in plain pytest —
webview_window.py is only a thin ObjC adapter over BridgeCore.

Protocol v1 (single "bridge" script-message channel, {type, payload} JSON):
  JS → PY  {type:"dirtyState",     payload:{dirty: bool}}
           {type:"pickKeyFile",    payload:{field: "sshKey"}}
           {type:"reconnectProxy", payload:{}}
           {type:"openPath",       payload:{kind: "captureDir"}}
  PY → JS  {type:"keyFilePicked", payload:{field, path}}
           delivered via window.__native.receive(<json>)

Design rules that keep the historical crash classes from recurring:
- json.dumps is the ONLY escaping layer; Python never interpolates JS source
  and never names a JS DOM selector (the JS side owns its DOM).
- handle_message never raises — the ObjC delegate must stay exception-free.
- JS owns dirty truth; Python mirrors it through typed messages only.
"""
import json
import logging

logger = logging.getLogger("magic-proxy.bridge")

# Fields the native side knows how to service with a file picker.
PICKABLE_FIELDS = frozenset({"sshKey"})

# Path kinds the native side may reveal (closed set, like PICKABLE_FIELDS —
# the JS side never names an arbitrary filesystem path to open).
OPENABLE_KINDS = frozenset({"captureDir"})

ACTION_SHOW_OPEN_PANEL = "showOpenPanel"
ACTION_RECONNECT_PROXY = "reconnectProxy"
ACTION_OPEN_PATH = "openPath"


def _plain(obj):
    """Recursively normalize ObjC-bridged containers (NSDictionary/NSArray)
    into plain Python dict/list. Anything unconvertible becomes {} / passes
    through unchanged — normalization never raises."""
    if isinstance(obj, dict):
        try:
            return {k: _plain(v) for k, v in obj.items()}
        except Exception:
            return {}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    keys = getattr(obj, "keys", None)
    if callable(keys):
        try:
            return {k: _plain(obj[k]) for k in obj.keys()}
        except Exception:
            return {}
    return obj


class BridgeCore:
    """Dict-in/dict-out bridge protocol. Never raises to the ObjC caller."""

    def __init__(self):
        self._dirty = False

    @property
    def dirty(self):
        """Close-guard state mirrored from the JS side."""
        return self._dirty

    def handle_message(self, msg):
        """Dispatch one bridge message (plain or ObjC-bridged); return actions."""
        try:
            return self._dispatch(_plain(msg))
        except Exception:
            logger.exception("bridge message dropped: %r", msg)
            return []

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            logger.warning("bridge message not a dict: %r", msg)
            return []
        mtype = msg.get("type")
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if mtype == "dirtyState":
            self._dirty = bool(payload.get("dirty"))
            return []
        if mtype == "pickKeyFile":
            field = payload.get("field")
            if field in PICKABLE_FIELDS:
                return [{"type": ACTION_SHOW_OPEN_PANEL, "field": field}]
            logger.warning("pickKeyFile for unknown field: %r", field)
            return []
        if mtype == "reconnectProxy":
            # Equivalent of the menu-bar 重新连接 item; the app-level handler
            # owns threading and the actual connection orchestration.
            return [{"type": ACTION_RECONNECT_PROXY}]
        if mtype == "openPath":
            kind = payload.get("kind")
            if kind in OPENABLE_KINDS:
                return [{"type": ACTION_OPEN_PATH, "kind": kind}]
            logger.warning("openPath for unknown kind: %r", kind)
            return []
        logger.warning("unknown bridge message type: %r", mtype)
        return []

    @staticmethod
    def build_fill_js(field, path):
        """Build JS delivering a picked path to the JS-owned receiver."""
        msg = {"type": "keyFilePicked", "payload": {"field": field, "path": path}}
        return "window.__native&&window.__native.receive(%s)" % json.dumps(
            msg, ensure_ascii=False)
