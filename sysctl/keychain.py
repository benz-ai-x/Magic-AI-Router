"""macOS Keychain helpers for SSH passwords (Security framework).

Uses SecItemAdd/SecItemCopyMatching/SecItemDelete directly so the password
never appears in a subprocess argv — the old `security -w <pw>` CLI path was
briefly visible in `ps` (#40).
"""
import logging

import Security

logger = logging.getLogger("magic-proxy.keychain")

SERVICE = "com.magic-proxy"


def _account(tunnel: dict) -> str:
    user = tunnel.get("ssh_user", "")
    host = tunnel.get("ssh_host", "")
    port = tunnel.get("ssh_port", 22)
    return f"{user}@{host}:{port}"


def _base_query(tunnel: dict) -> dict:
    return {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: SERVICE,
        Security.kSecAttrAccount: _account(tunnel),
    }


def set_password(tunnel: dict, password: str) -> bool:
    if not tunnel.get("ssh_host"):
        return False
    try:
        # Replace any existing entry so -U semantics (update-in-place) hold.
        Security.SecItemDelete(_base_query(tunnel))
        attrs = dict(_base_query(tunnel))
        attrs[Security.kSecValueData] = password.encode("utf-8")
        status = Security.SecItemAdd(attrs, None)
        ok = status[0] == Security.errSecSuccess if isinstance(status, tuple) \
            else status == Security.errSecSuccess
        if not ok:
            # Never log the password; the query attrs hold no secret value.
            logger.warning("Keychain set failed: SecItemAdd status %s", status)
        return ok
    except Exception as e:  # noqa: BLE001 — keychain must never raise to UI
        logger.warning("Keychain set failed: %s", type(e).__name__)
        return False


def get_password(tunnel: dict) -> str:
    if not tunnel.get("ssh_host"):
        return ""
    try:
        query = dict(_base_query(tunnel))
        query[Security.kSecReturnData] = True
        query[Security.kSecMatchLimit] = Security.kSecMatchLimitOne
        status, data = Security.SecItemCopyMatching(query, None)
        if status == Security.errSecSuccess and data is not None:
            return bytes(data).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain get failed: %s", type(e).__name__)
    return ""


def delete_password(tunnel: dict) -> None:
    if not tunnel.get("ssh_host"):
        return
    try:
        Security.SecItemDelete(_base_query(tunnel))
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain delete failed: %s", type(e).__name__)
