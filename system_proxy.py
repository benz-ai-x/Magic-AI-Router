"""macOS system proxy control via networksetup.

Transactional: sys_proxy_controller converges through snapshot() /
apply_transaction() / release_transaction(); recover_stale_transaction()
repairs after a crash. That transaction interface is the entire public
surface — there is no stateless apply/clear path.
"""
import logging
import json
import os
import subprocess

import config_store

logger = logging.getLogger("magic-proxy.system_proxy")

DEFAULT_BYPASS = ["*.local", "169.254/16", "127.0.0.1", "localhost"]
_TIMEOUT = 5
JOURNAL_PATH = os.path.expanduser("~/.magic-proxy-system-proxy-journal.json")


def _run(args):
    """Run a networksetup command. Returns (ok, stderr). Never raises."""
    try:
        cp = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    if cp.returncode != 0:
        return False, (cp.stderr or "").strip()
    return True, ""


def _active_services():
    """Return list of enabled network service names (skips disabled '*' lines)."""
    # Bypass _run here: we need stdout, which _run discards on success.
    try:
        cp = subprocess.run(
            ["networksetup", "-listallnetworkservices"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if cp.returncode != 0:
        return []
    services = []
    for i, line in enumerate(cp.stdout.splitlines()):
        if i == 0:
            continue  # header/notice line
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        services.append(line)
    return services


def _get(args):
    """Return stdout for a successful networksetup query, else None."""
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def _parse_proxy(output):
    values = {}
    for line in (output or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return {
        "enabled": values.get("Enabled", "No") == "Yes",
        "host": values.get("Server", ""),
        "port": values.get("Port", "0"),
    }


def snapshot():
    """Capture proxy state for active services before Magic AI Router changes it.

    The caller owns this in-memory snapshot and passes it to ``restore``. We
    intentionally do not touch a service whose state cannot be read: blindly
    disabling an unknown corporate proxy is unsafe.
    """
    return snapshot_services(_active_services())


def snapshot_services(services):
    state = {}
    for svc in services:
        web = _get(["networksetup", "-getwebproxy", svc])
        secure = _get(["networksetup", "-getsecurewebproxy", svc])
        bypass = _get(["networksetup", "-getproxybypassdomains", svc])
        if web is None or secure is None or bypass is None:
            logger.warning("system_proxy snapshot skipped unreadable service: %s", svc)
            continue
        state[svc] = {
            "web": _parse_proxy(web), "secure": _parse_proxy(secure),
            "bypass": [
                line.strip() for line in bypass.splitlines()
                if line.strip() and not line.startswith("There aren't any")
            ],
        }
    return state


def restore(state):
    """Restore a previously captured state; never clear unrelated services."""
    if not state:
        return True, ""
    errors = []
    for svc, saved in state.items():
        commands = []
        for kind, set_flag, state_flag in (
            ("web", "-setwebproxy", "-setwebproxystate"),
            ("secure", "-setsecurewebproxy", "-setsecurewebproxystate"),
        ):
            item = saved[kind]
            if item["enabled"]:
                commands.extend((
                    ["networksetup", set_flag, svc, item["host"], str(item["port"])],
                    ["networksetup", state_flag, svc, "on"],
                ))
            else:
                commands.append(["networksetup", state_flag, svc, "off"])
        bypass = saved.get("bypass") or ["Empty"]
        commands.append(["networksetup", "-setproxybypassdomains", svc, *bypass])
        for cmd in commands:
            ok, err = _run(cmd)
            if not ok:
                errors.append(f"{svc}: {err}")
    return (not errors), "; ".join(errors)


def _desired_state(services, host, port, bypass):
    return {
        svc: {
            "web": {"enabled": True, "host": host, "port": str(port)},
            "secure": {"enabled": True, "host": host, "port": str(port)},
            "bypass": sorted(bypass),
        }
        for svc in services
    }


def _write_journal(original, desired):
    """Persist the recovery journal atomically via config_store.atomic_write.

    Returns True on success, False on failure (atomic_write already cleaned
    up the temp file and logged the OSError — no raise).
    """
    payload = json.dumps(
        {"version": 1, "original": original, "desired": desired})
    return config_store.atomic_write(JOURNAL_PATH, payload)


def _remove_journal():
    try:
        os.unlink(JOURNAL_PATH)
    except FileNotFoundError:
        pass


def _state_matches(current, expected):
    if set(current) != set(expected):
        return False
    for svc, wanted in expected.items():
        got = current.get(svc, {})
        for kind in ("web", "secure"):
            if got.get(kind) != wanted.get(kind):
                return False
        if sorted(got.get("bypass", [])) != sorted(wanted.get("bypass", [])):
            return False
    return True


def apply_transaction(host, port, bypass, original):
    """Apply to every snapshotted service or rollback every touched service."""
    if not original:
        return False, "no readable network service snapshots", None
    services = list(original)
    desired = _desired_state(services, host, port, bypass)
    if not _write_journal(original, desired):
        return False, "could not write recovery journal", None

    errors = []
    for svc in services:
        commands = (
            ["networksetup", "-setwebproxy", svc, host, str(port)],
            ["networksetup", "-setwebproxystate", svc, "on"],
            ["networksetup", "-setsecurewebproxy", svc, host, str(port)],
            ["networksetup", "-setsecurewebproxystate", svc, "on"],
            ["networksetup", "-setproxybypassdomains", svc, *(bypass or ["Empty"])],
        )
        for cmd in commands:
            ok, err = _run(cmd)
            if not ok:
                errors.append(f"{svc}: {err}")
                break
        if errors:
            break
    if errors:
        rolled_back, rollback_err = restore(original)
        if rolled_back:
            _remove_journal()
        else:
            errors.append(f"rollback failed: {rollback_err}")
        return False, "; ".join(errors), (None if rolled_back else desired)
    return True, "", desired


def release_transaction(original, desired):
    """Restore only while settings still equal the values written by us."""
    current = snapshot_services(list(original))
    if not _state_matches(current, desired):
        return False, "network proxy changed externally; refusing to overwrite it"
    ok, err = restore(original)
    if ok:
        _remove_journal()
    return ok, err


def recover_stale_transaction():
    """Recover an interrupted previous run using compare-and-restore semantics."""
    if not os.path.exists(JOURNAL_PATH):
        return True, ""
    try:
        with open(JOURNAL_PATH) as fh:
            journal = json.load(fh)
        original = journal["original"]
        desired = journal["desired"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"invalid system proxy recovery journal: {exc}"
    return release_transaction(original, desired)


