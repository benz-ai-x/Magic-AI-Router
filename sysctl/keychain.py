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
    """凭证账户名：优先稳定 id（issue #8）；无 id 时为 legacy 推导。"""
    stable_id = tunnel.get("id")
    if stable_id:
        return f"tunnel:{stable_id}"
    return _legacy_account(tunnel)


def _legacy_account(tunnel: dict) -> str:
    user = tunnel.get("ssh_user", "")
    host = tunnel.get("ssh_host", "")
    port = tunnel.get("ssh_port", 22)
    return f"{user}@{host}:{port}"


def _base_query(tunnel: dict, account: str | None = None) -> dict:
    return {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: SERVICE,
        Security.kSecAttrAccount: account or _account(tunnel),
    }


def set_password(tunnel: dict, password: str) -> bool:
    if not tunnel.get("ssh_host"):
        return False
    try:
        # Replace any existing entry so -U semantics (update-in-place) hold.
        final_account = _account(tunnel)
        Security.SecItemDelete(_base_query(tunnel, final_account))
        attrs = _base_query(tunnel, final_account)
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
    accounts = [_account(tunnel)]
    legacy = _legacy_account(tunnel)
    if legacy not in accounts:
        accounts.append(legacy)  # 迁移期回退读（issue #8）
    try:
        for account in accounts:
            query = _base_query(tunnel, account)
            query[Security.kSecReturnData] = True
            query[Security.kSecMatchLimit] = Security.kSecMatchLimitOne
            status, data = Security.SecItemCopyMatching(query, None)
            if status == Security.errSecSuccess and data is not None:
                return bytes(data).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain get failed: %s", type(e).__name__)
    return ""


def delete_legacy_password(tunnel: dict) -> bool:
    """仅清 legacy 账户（user@host:port）——re-pin 收敛用，不动 id 账户。"""
    try:
        Security.SecItemDelete(_base_query(tunnel, _legacy_account(tunnel)))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain legacy delete failed: %s", type(e).__name__)
        return False


def delete_password(tunnel: dict) -> bool:
    """删除隧道密码。返回是否成功（条目本就不存在视为成功）。"""
    if not tunnel.get("ssh_host"):
        return True
    try:
        for account in {_account(tunnel), _legacy_account(tunnel)}:
            Security.SecItemDelete(_base_query(tunnel, account))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Keychain delete failed: %s", type(e).__name__)
        return False
