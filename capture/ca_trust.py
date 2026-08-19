"""Root CA trust detection + first-run guidance window (ADR-001 Task 4 AC-3).

Trusts ONLY the public leaf certificate mitmdump generates on first launch
into its default confdir (~/.mitmproxy/mitmproxy-ca-cert.pem). The private
key (mitmproxy-ca.pem) is never read, referenced, or shipped -- Global
Constraint 3 (root CA private key stays local, never in the bundle).

Detection is fail-closed: any ambiguity (missing cert, subprocess error,
timeout) is treated as "not trusted" so the guidance flow always runs rather
than silently assuming a state we couldn't actually confirm.

Trusting is a genuine macOS security-store mutation and always goes through
`security add-trusted-cert`, which triggers an interactive OS admin password
prompt -- that prompt is the actual user-consent gate and cannot be
bypassed by this code (see ADR-001 §证书信任 UX). This module never calls
it without the user first clicking "信任并继续" in CATrustGuideWindow.
"""
import logging
import os
import subprocess

import objc
from AppKit import (
    NSApp, NSButton, NSFont, NSMakeRect, NSScreen, NSTextView,
    NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject

logger = logging.getLogger("magic-proxy.ca_trust")

def _ca_cert_path():
    """Compute the CA cert path at call time so a HOME change after import
    (e.g. tests that chdir / monkeypatch) is respected. Mirrors the
    capture_store._home_dir() lazy-eval pattern."""
    return os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")


def _login_keychain():
    """Same lazy-eval rationale as _ca_cert_path()."""
    return os.path.expanduser("~/Library/Keychains/login.keychain-db")


_VERIFY_TIMEOUT = 10
_TRUST_TIMEOUT = 60

GUIDE_TEXT = (
    "开启抓包模式需要信任本地生成的根证书，才能在你自己的电脑上解密自己发起的 "
    "HTTPS 请求以便分析。\n\n"
    "证书私钥永远只保存在本机，不会被打包、不会上传、不会离开这台电脑。\n\n"
    "抓包内容可能包含 system prompt、用户输入和模型回复，将以明文 JSONL "
    "保存在 ~/.magic-proxy-captures/，仅当前 macOS 用户可读取。请只捕获你有权分析的流量。\n\n"
    "点击“信任并继续”后，macOS 会弹出密码确认框——这是系统自带的安全"
    "机制，无法被本应用绕过。\n\n"
    "如何撤销信任：打开“钥匙串访问” App → 登录钥匙串 → 找到 "
    "“mitmproxy” → 双击 → 展开“信任” → 改为“没有信任”"
    "或直接删除该证书。"
)

# Strong refs to live guide-window instances -- mirrors prefs.py's
# _active_prefs: NSWindow delegates/targets are weak, so without this the
# Python wrapper is GC'd as soon as show_ca_trust_guide() returns.
_active_guides = set()


def _button(parent, title, x, y, w, h, target, action, default=False):
    """Mirrors prefs.py's _push_button exactly (same helper shape, kept
    local to avoid reaching into another module's "private" function)."""
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    b.setTitle_(title)
    b.setBezelStyle_(1)
    b.setTarget_(target)
    b.setAction_(action)
    if default:
        b.setKeyEquivalent_("\r")
    parent.addSubview_(b)
    return b


def ca_cert_exists() -> bool:
    return os.path.exists(_ca_cert_path())


def is_trusted() -> bool:
    """True iff the CA cert is present AND the trust store actually trusts
    it (`security verify-cert` is the authoritative signal per product-lead
    guidance -- presence in the keychain alone does not imply trust)."""
    if not ca_cert_exists():
        return False
    try:
        cp = subprocess.run(
            ["security", "verify-cert", "-c", _ca_cert_path(), "-l"],
            capture_output=True, text=True, timeout=_VERIFY_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("CA trust check failed: %s", e)
        return False
    return cp.returncode == 0


def trust_ca():
    """Add the CA cert to the login keychain's trust settings. Blocks on the
    macOS admin password prompt. Returns (ok, err); never raises."""
    try:
        cp = subprocess.run(
            ["security", "add-trusted-cert", "-d", "-r", "trustRoot",
             "-k", _login_keychain(), _ca_cert_path()],
            capture_output=True, text=True, timeout=_TRUST_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    if cp.returncode != 0:
        return False, (cp.stderr or "").strip()
    return True, ""


def reveal_ca_cert() -> bool:
    """Manual-trust fallback: reveal the cert file in Finder so the user can
    open Keychain Access and set trust themselves."""
    try:
        subprocess.Popen(["open", "-R", _ca_cert_path()])
        return True
    except OSError:
        logger.exception("Failed to reveal CA cert in Finder")
        return False


def attempt_trust():
    """Decision logic behind the "信任并继续" button, kept as a plain
    function so it's testable without touching PyObjC. Re-verifies after
    trust_ca() succeeds rather than trusting add-trusted-cert's own exit
    code alone (defense in depth). Returns (ok, err)."""
    ok, err = trust_ca()
    if not ok:
        return False, err
    if not is_trusted():
        return False, "信任设置未生效，请重试，或改用“手动设置”。"
    return True, ""


class CATrustGuideWindow(NSObject):
    """First-run CA trust guidance window (mirrors prefs.py's PrefsWindow
    NSObject-subclass pattern)."""

    def init(self):
        self = objc.super(CATrustGuideWindow, self).init()
        if self is None:
            return None
        self._on_result = None  # callable(bool trusted_now)
        self._window = None
        return self

    def show(self, on_result=None):
        self._on_result = on_result

        w, h = 460, 380
        screen = NSScreen.mainScreen()
        if screen is not None:
            sf = screen.visibleFrame()
            cx = sf.origin.x + (sf.size.width - w) / 2
            cy = sf.origin.y + (sf.size.height - h) / 2
        else:
            cx, cy = 100, 100

        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(cx, cy, w, h),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable, 2, False,
        )
        win.setTitle_("信任抓包根证书")
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        self._window = win
        c = win.contentView()

        margin = 20
        text_h = h - 2 * margin - 44
        text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(margin, margin + 44, w - 2 * margin, text_h)
        )
        text_view.setString_(GUIDE_TEXT)
        text_view.setFont_(NSFont.systemFontOfSize_(13))
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setDrawsBackground_(False)
        c.addSubview_(text_view)

        btn_w, btn_h = 110, 30
        gap = 8
        y = margin
        self._trust_btn = _button(
            c, "信任并继续", w - margin - btn_w, y, btn_w, btn_h,
            self, "doTrust:", default=True,
        )
        self._manual_btn = _button(
            c, "手动设置", w - margin - 2 * btn_w - gap, y, btn_w, btn_h,
            self, "doManual:",
        )
        self._cancel_btn = _button(c, "取消", margin, y, 80, btn_h, self, "doCancel:")

        win.makeKeyAndOrderFront_(None)
        NSApp.setActivationPolicy_(0)
        NSApp.activateIgnoringOtherApps_(True)

    # ── actions ──────────────────────────────────────────────────────────
    # PyObjC validates every NSObject-subclass method's trailing-underscore
    # count against its positional-arg count -- including private helpers,
    # NOT just button actions (CLAUDE.md: method names must not start with
    # a single underscore, or PyObjC's selector machinery misparses them;
    # confirmed empirically: a leading-underscore, zero-trailing-underscore
    # helper taking 1 extra arg raised objc.BadPrototypeError at class
    # definition time). finish_ takes one arg -> exactly one trailing `_`,
    # no leading `_`, mirroring prefs.py's doSave_/onAuthChange_ pattern.

    def doTrust_(self, sender):
        ok, err = attempt_trust()
        if ok:
            self.finish_(True)
        else:
            logger.warning("CA trust attempt failed: %s", err)
            self._trust_btn.setTitle_("重试信任")

    def doManual_(self, sender):
        reveal_ca_cert()
        self.finish_(False)

    def doCancel_(self, sender):
        self.finish_(False)

    def windowWillClose_(self, notification):
        _active_guides.discard(self)

    def finish_(self, trusted):
        if self._window is not None:
            self._window.close()
        _active_guides.discard(self)
        if callable(self._on_result):
            self._on_result(trusted)


def show_ca_trust_guide(on_result=None):
    w = CATrustGuideWindow.alloc().init()
    _active_guides.add(w)
    w.show(on_result)
    return w
