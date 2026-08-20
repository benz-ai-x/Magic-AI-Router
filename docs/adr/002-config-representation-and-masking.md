# ADR-002: 配置表示收敛与 API key 掩码布尔契约

- 状态：Accepted
- 日期：2026-08-12
- 决策者：tech-lead（用户确认）
- 原编号：ADR-023（2026-08-19 仓库重建后重编号压缩间隙）
- 影响范围：config.py、suanpan/config.py、config_server.py、provider_auth.py、config_ui.html、config_store.py、claude_code_setup.py

## 上下文

2026-08-12 的架构审查（4 个「强烈建议」候选）落地了两个有长期约束力的配置契约变更。它们改变了**持久化文件格式**和**跨进程 UI 契约**，未来架构审查若不了解背景，可能重新建议已被否决的旧方案（如"给 UI 发掩码字符串"）。本 ADR 固化这两条决策及否决理由。

## 决策

### 决策 1：监听地址存端口字段，不存 "host:port" 字符串

`~/.magic-proxy.json` 的 `http_listen` 和 `~/.suanpan.yaml` 的 `listen` 从 `"host:port"` 字符串收敛为整型端口字段：`http_listen_port` / `listen_port`。

- **理由**：host 恒为 loopback（mp 侧有 `netloc.require_loopback` 校验；suanpan 侧本次补齐校验——网关是本机服务，本就不该监听非 loopback）。端口字段足以表达，字符串格式让"存储↔UI↔运行时"之间需要 `config_server._derive_ports` / `_apply_port_edits` 两个纯翻译函数，每次保存翻译 4 遍。
- **重组点唯一**：`"host:port"` 字符串只在 `netloc.parse_listen` / `format_listen` 调用点重组（proxy.py 监听、suanpan uvicorn、capture upstream、chromium_proxy），不再作为存储格式。
- **迁移策略（读时兼容，不写一次性迁移脚本）**：
  - mp 侧 `config.merge_config`：读到旧 `http_listen` 字符串则 parse 出端口转 `http_listen_port`。
  - suanpan 侧 `AppConfig`：pydantic `mode="before"` validator 把旧 `listen` 字符串规整为 `listen_port`（含 loopback 校验，非法值回退默认）。
  - 保存时一律写新端口字段。用户无需手动改配置，下次保存自动落新格式。

### 决策 2：真实 API key 不出进程，UI 契约用 `api_key_set` 布尔

Suanpan provider 的 `api_key` 发给设置窗（WKWebView）时，不再发掩码字符串（`•••••XXXX`），改为 `api_key: null` + `api_key_set: bool`。

- **理由**：真实 key 从不离开 Python 进程是更强的安全边界；且旧的 `•` 掩码字符曾是**四层链路**（存储 / `save_config_dict` 保存判断 / `provider_auth.resolve_api_key` 运行时判断 / JS `startsWith('•')`）的关键判断依据，却有两个独立常量（`suanpan/config.py:_MASK`、`provider_auth.py:_MASK_PREFIX`）加一个硬编码 JS 字面量——改字符会静默破坏四层。
- **保存契约**：UI 未修改 key 时输入框留空，`collectProvider` 不写 `api_key`、`api_key_set` 随 state 回传为 true → `save_config_dict` 的 `_restore_key(keep=api_key_set)` 据此保留旧 key；用户输入新值则 `api_key_set` 置 true 并用新值。
- **删除**：`_MASK`、`_mask_key`、`provider_auth._MASK_PREFIX`、所有 `startswith("•")` / `startsWith('•')` 判断。`resolve_api_key` 不再判掩码（掩码串已不存在）。

## 否决方案（未来审查勿重提）

| 方案 | 否决理由 |
|---|---|
| 给 UI 发掩码字符串（`•••••XXXX`） | 真实 key 出进程 + 掩码字符成四层关键判断、多常量定义，脆弱且不必要。已被 `api_key_set` 布尔取代 |
| 一次性迁移脚本（启动时改写旧格式文件） | 读时兼容 + 保存落新格式即可无感迁移，无需额外脚本和迁移状态 |
| 监听地址保留完整 "host:port" 字符串 | host 恒 loopback，字符串格式逼出两个纯翻译函数；端口字段足够且消除翻译层 |

## 影响

- **持久化格式**：旧格式配置文件读时兼容，无 breaking change。
- **UI 契约**：`config_server /api/state` 返回的 provider 不再含 `api_key` 真值/掩码，改含 `api_key_set`。
- **测试**：`config_store.PATHS` 新增 `claude_settings` 条目（`~/.claude/settings.json`），测试可 `patch.dict` 重定向，消除写真实文件的风险（同候选 3 的 `claude_code_setup.py` 抽取）。
- **已知边界（原契约既有，未回归）**：设置窗输入框留空 = 不修改 key，无法表达"删除已保存的 key"。

## 相关提交

- `b1aae54` 候选 1：ServiceCoordinator 瘦身（与本 ADR 契约无关，纯内部重构）
- `d4337ac` 候选 3：原子写收敛 config_store + claude_code_setup 独立
- `9f5e153` 候选 2：http_listen/listen 端口字段
- `0a1a9c3` 候选 4：API key 掩码布尔契约

## 增补（2026-08-20，issue #6）：保存事务与错误语义

- 持久化唯一事务边界为 `mpconf/config_state.py::ConfigStateStore`：load（missing/valid/invalid/io_error 四态，损坏不再折叠成空）→ prepare（数值/URL/跨引用全量校验 + Keychain 变更计划派生，密码剥离出候选）→ commit（journal 载荷内嵌 → MP → SP → Keychain → 清 journal → 回调）。
- `on_sp_saved` 回调只在完整提交后触发；失败返回结构化阶段（validate/journal/mp/sp/keychain）且错误不含 secret。
- 跨文件提交崩溃由启动时 `recover()` 幂等重放 journal 补齐。
- invalid 主文件不覆盖最后已知良好的 `.bak`；首创建与保存共用 0600/0700 权限路径。

## 增补（2026-08-20，issue #8）：稳定 id 与凭证所有权

- Tunnel 持不可变 `id`（`t-<sha1(user@host:port)[:10]>`，装载期确定性迁移赋值；重复身份/重复 id 抛可行动错误，不猜 secret 归属）；Keychain 账户优先 `tunnel:<id>`，无 id 时 legacy `user@host:port` 仅作迁移期回退读。重命名/改地址不改 id 不丢密码。
- Provider 持 `id`（`p-<sha1(name)[:10]>`）；api_key 的 keep/replace/clear（`_restore_key`）按 id 匹配旧值——重命名保住 key，id 不同绝不按名串接。legacy 无 id 旧档按名回退。
- 旧 Keychain 条目迁移：password 隧道在下次提交时经 ConfigStateStore.prepare 的 re-pin 迁到 id 账户。re-pin 只在 id==当前身份哈希时读 legacy（身份编辑过的隧道绝不读——legacy 账户可能属于别的实体，不猜归属）；收敛后 legacy-only 删除随事务完成。删除隧道时双账户清理随事务执行。迁移错误（重复身份/重复 id）为 IdentityMigrationError：不触发 .bak 隔离、UI 侧降级为 _load_error 提示，绝不静默猜测 secret 归属。
