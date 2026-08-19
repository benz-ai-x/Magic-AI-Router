"""WKWebView window for embedding the web-based config UI.

Thin ObjC adapter over bridge_protocol.BridgeCore: owns only the NSWindow /
WKWebView / NSOpenPanel wiring. All protocol logic — message dispatch, dirty
state, JS construction — lives in bridge_protocol.py (pure Python, testable).

Design rules enforced here:
- One script-message channel named "bridge"; the delegate only decodes and
  forwards dicts to BridgeCore, never implementing protocol logic itself.
- Python ships data to JS exclusively via BridgeCore.build_fill_js() —
  no hand-built JS source, no DOM selector knowledge on this side.
- Nothing raises through an ObjC callback: every native entry point
  catches and logs.

WKWebView lookup is deferred to call time so that importing this module (via
app.py's import chain) never fails in headless/test environments where the
WebKit framework is not loaded.
"""
import logging
import os

import objc
from AppKit import (
    NSAlert, NSAlertFirstButtonReturn, NSApp, NSMakeRect, NSOpenPanel,
    NSScreen,
    NSWindow, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSBundle, NSURL, NSURLRequest

from bridge_protocol import ACTION_SHOW_OPEN_PANEL, BridgeCore

logger = logging.getLogger("magic-proxy.webview")

# NSAutoresizingMaskValues (not exposed as NSView class attrs in PyObjC)
_WidthSizable = 2
_HeightSizable = 16

_WEBKIT_LOADED = False
_config_window = None
_webview = None
_window_delegate = None


def _ensure_webkit():
    """Load WebKit framework and return the WKWebView class."""
    global _WEBKIT_LOADED
    if not _WEBKIT_LOADED:
        NSBundle.bundleWithPath_(
            "/System/Library/Frameworks/WebKit.framework").load()
        _WEBKIT_LOADED = True
    return objc.lookUpClass("WKWebView")


class _ConfigWindowDelegate(NSObject):
    """NSWindowDelegate + WKScriptMessageHandler: forwards to BridgeCore."""

    def init(self):
        self = objc.super(_ConfigWindowDelegate, self).init()
        if self is None:
            return None
        self._core = BridgeCore()
        self._on_action = None
        return self

    def setActionHandler_(self, handler):
        """App-level action sink (reconnectProxy / openPath); may be None."""
        self._on_action = handler

    def windowShouldClose_(self, _notification):
        """Synchronous close-guard over the mirrored dirty state."""
        if not self._core.dirty:
            return True
        alert = NSAlert.alloc().init()
        alert.setMessageText_("有未保存的更改")
        alert.setInformativeText_("关闭窗口将丢失未保存的更改，确定要关闭吗？")
        alert.addButtonWithTitle_("放弃更改并关闭")
        alert.addButtonWithTitle_("取消")
        alert.setAlertStyle_(1)  # NSWarningAlertStyle
        return alert.runModal() == NSAlertFirstButtonReturn

    # WKScriptMessageHandler — single "bridge" channel; core normalizes
    # ObjC-bridged containers (NSDictionary payload) itself.
    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        if message.name() != "bridge":
            return
        try:
            body = message.body()
        except Exception:
            logger.warning("undecodable bridge message", exc_info=True)
            return
        for action in self._core.handle_message(body):
            if action.get("type") == ACTION_SHOW_OPEN_PANEL:
                self.showOpenPanelFill_(action["field"])
            elif self._on_action:
                try:
                    self._on_action(action)
                except Exception:
                    logger.exception("bridge action handler failed")

    def showOpenPanelFill_(self, field):
        """Show NSOpenPanel; ship the picked path to the JS-owned receiver."""
        try:
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(False)
            ssh_dir = os.path.expanduser("~/.ssh")
            if os.path.isdir(ssh_dir):
                panel.setDirectoryURL_(NSURL.fileURLWithPath_(ssh_dir))
            if panel.runModal() == 1:
                url = panel.URL()
                if url and _webview:
                    js = BridgeCore.build_fill_js(field, url.path())
                    _webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            logger.exception("key-file picker failed")


def show_config_window(url, title="Magic AI Router 设置", on_action=None):
    """Open (or focus) the config webview window.

    on_action: optional callable receiving app-level bridge actions
    ({type: "reconnectProxy"} / {type: "openPath", kind: ...}) — the adapter
    keeps handling UI-local actions (open panel) itself.
    """
    global _config_window, _webview, _window_delegate

    if _config_window and _config_window.isVisible():
        _config_window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        return

    # Release the prior window/webview pair before overwriting refs
    # (NSWindow releasedWhenClosed:NO retains until explicitly closed).
    if _config_window:
        _config_window.close()
        _config_window = None
        _webview = None

    WKWebView = _ensure_webkit()
    WKWebViewConfiguration = objc.lookUpClass("WKWebViewConfiguration")

    w, h = 1080, 880
    screen = NSScreen.mainScreen()
    if screen:
        sf = screen.visibleFrame()
        cx = sf.origin.x + (sf.size.width - w) / 2
        cy = sf.origin.y + (sf.size.height - h) / 2
    else:
        cx, cy = 100, 100

    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(cx, cy, w, h),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable,
        2, False,
    )
    win.setTitle_(title)
    win.setReleasedWhenClosed_(False)
    win.setMinSize_((880, 600))

    _window_delegate = _ConfigWindowDelegate.alloc().init()
    _window_delegate.setActionHandler_(on_action)
    win.setDelegate_(_window_delegate)

    config = WKWebViewConfiguration.alloc().init()
    config.userContentController().addScriptMessageHandler_name_(
        _window_delegate, "bridge")

    cv = win.contentView()
    _webview = WKWebView.alloc().initWithFrame_configuration_(cv.bounds(), config)
    _webview.setAutoresizingMask_(_WidthSizable | _HeightSizable)
    _webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    cv.addSubview_(_webview)

    win.makeKeyAndOrderFront_(None)
    # NSApp.setActivationPolicy_(0) would promote LSUIElement=true app to
    # regular (Dock-visible) — skipped to avoid Dock icon flicker.
    NSApp.activateIgnoringOtherApps_(True)

    _config_window = win
