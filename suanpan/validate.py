"""sp（suanpan）配置的分域校验器（架构评审候选 2：prepare 收缩为
orchestrator）。

输入是未经 merge 的保存候选；输出中文错误行（文案与顺序被
test_config_state 经 prepare 钉死）。schema 形状知识在本域
（suanpan.config 的 pydantic schema + friendly_config_error_lines），
路由目标文法单一所有者在本域（suanpan.router.parse_route_target）——
校验器与被校验的知识同域演进（locality）。

本模块经 config_state.prepare 的 **lazy import** 进入（仅 sp 侧参与
保存时才加载）：pydantic 缺席的宿主（ADR-000）保存 mp 不受影响，
schema 段在 ImportError 下降级跳过。
"""
from urllib.parse import urlsplit

from shared.defaults import PORT_MAX as _PORT_MAX

_BODY_LIMIT_MAX = 512        # MB
_REQUEST_TIMEOUT_MAX = 86400


def _valid_http_origin(url) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and not parts.query and not parts.fragment)


def _schema_error_lines(exc) -> list:
    """schema 校验错误行——格式化单一归宿 suanpan.friendly_config_error_lines。"""
    from suanpan.config import friendly_config_error_lines
    return [f"schema 校验失败 {seg}"
            for seg in friendly_config_error_lines(exc)]


def sp_errors(sp) -> list:
    """sp 候选的全量校验：顶层数值 + pydantic schema + 供应商 URL +
    路由引用完整性。"""
    errors = []
    lp = sp.get("listen_port")
    if lp is not None and (not isinstance(lp, int)
                           or not 1 <= lp <= _PORT_MAX):
        errors.append("listen_port 端口无效（须 1..65535）")
    # 顶层字段（#46 T1b：旧代码查不存在的 server 键——死校验分支，
    # 顶层非法值静默落盘）。schema 见 suanpan/config.py AppConfig。
    timeout = sp.get("request_timeout_s")
    if timeout is not None and (not isinstance(timeout, int)
                                or not 0 < timeout <= _REQUEST_TIMEOUT_MAX):
        errors.append(f"request_timeout_s 无效（须 >0 且 ≤{_REQUEST_TIMEOUT_MAX}）")
    body_limit = sp.get("body_limit_mb")
    if body_limit is not None and (not isinstance(body_limit, int)
                                   or not 0 < body_limit <= _BODY_LIMIT_MAX):
        errors.append(f"body_limit_mb 无效（须 >0 且 ≤{_BODY_LIMIT_MAX}）")
    # pydantic schema 校验并入事务路径（#46 T1b）：旧径 commit 只有
    # yaml.safe_dump，schema 非法但手检放行的配置会直写磁盘、下次启动
    # load_config 才炸。deps 缺席时跳过（ADR-000 lazy-import：无网关
    # 依赖的宿主仍可保存 mp）。
    try:
        from suanpan.config import AppConfig as _SpSchema
    except ImportError:
        _SpSchema = None
    if _SpSchema is not None:
        try:
            _SpSchema.model_validate(sp)
        except Exception as _exc:  # noqa: BLE001 — schema 错误形状不可枚举
            errors.extend(_schema_error_lines(_exc))
    providers = sp.get("providers") or {}
    for name, p in providers.items():
        if not _valid_http_origin((p or {}).get("base_url", "")):
            errors.append(f"供应商 {name} 的 base_url 必须是合法 http(s) origin")
    routes = set()
    rules = sp.get("rules")
    if rules is not None and not isinstance(rules, list):
        errors.append("rules 必须是列表")
        rules = []
    # 路由目标文法单一所有者（#70 S1）：parse_route_target 消费路由侧
    # 双分隔符（"/" 优先 "," 兜底）——不再 split("/") 手写
    from suanpan.router import parse_route_target as _parse_route
    for r in rules or []:
        if not isinstance(r, dict):
            # 形状守卫（#69 S11）：元素非 dict（如 "abc"）时 schema 校验
            # 已记错，此处不得再对它 .get() 裸抛
            continue
        target = r.get("route_to", "")
        routes.add(_parse_route(str(target))[0])
    router = sp.get("router")
    if router is not None and not isinstance(router, dict):
        errors.append("router 必须是映射")
        router = {}
    _default_target = (router or {}).get("default") or ""
    if _default_target:
        routes.add(_parse_route(str(_default_target))[0])
    routes.discard("")
    for prov in sorted(routes):
        if prov and prov not in providers:
            errors.append(f"route_to/default 引用了不存在的供应商：{prov}")
    return errors
