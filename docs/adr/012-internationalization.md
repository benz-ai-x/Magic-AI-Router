# ADR-012: 界面国际化（i18n）

日期：2026-09-25
状态：已接受（M0+M1 随本批落地；M2–M5 见文末路线图）

## 背景

产品面向中英双语用户（菜单、设置窗、通知、错误提示全中文硬编码，
~600 个可提取键位）。目标：菜单与配置面中英文可切换，即时生效。

## 决策（D1–D7）

1. **D1 文案载体 = 自研语义键 catalog，不用 gettext**：`shared/locales/zh-CN.json` + `en.json` + 叶子模块 `shared/i18n.py`（`t(key, **params)` 唯一取词口）。理由：零新依赖；Python 菜单侧与 JS 设置窗侧（M2 serve 时注入）同源；PyInstaller 走现有 `--add-data` 平铺管线。i18n **不得 import util**（同层不同域，`test_arch_imports`），目录查找自包含（dev 包内子目录 / frozen `_MEIPASS` 根）。
2. **D2 语义键**（`menu.group.proxy` / `notify.forward.enabled.title`），不用英文原文作 msgid——grep 得到、守卫可钉、改文案不炸键。
3. **D3 语言偏好存 mp 配置顶层 `language: "auto" | "zh-CN" | "en"`，缺省 `zh-CN`**。`auto` 读 AppleLanguages（plistlib 直读 `.GlobalPreferences.plist`，无子进程）。**缺省 zh-CN 使全部钉死中文文案的存量测试零改动**——en 的正确性由守卫保证。**语言键不校验**（validate/JS 镜像零新增规则）：`i18n.resolve` 全兜底，坏值落缺省。
4. **D4 即时切换，无重启**：`MenuState.language`（resolved 值）进 `struct_key()` → 语言翻转走既有整树重建机制，中文只在渲染层（PR #100 后菜单刷新按稳定 ref_key 而非标题，语言切换无字典键坑）。设置窗侧 M2 做 serve 时注入 + reload。
5. **D5 服务端生成的错误消息两步走**：v1 生成时按当前语言渲染（语言是全局配置）；M3（可选，单独拍板）校验器改结构化错误 `{code, params}` 渲染移到边界。
6. **D6 不翻译清单**：日志（守卫豁免 logging 调用子树）、suanpan 网关 API wire 错误（开发面，英文惯例可后续单议）、内部标识符（如 `.cc-badge-新增` CSS 类名）、代码注释、docs。README 英文化是独立内容轨道。
7. **D7 `agent_instructions` 跟随应用语言**（M4 实施）。

## 守卫（tests/test_i18n.py）

1. **键位奇偶**：zh/en 键集合一致、值非空、占位符逐键一致（多重集）；
2. **en 全译**：en 值不含汉字（语言自称用英文名，如 Chinese (Simplified)）；
3. **取词守卫**：产品代码 `i18n.t("...")` 的字面键必须在 catalog 内——**动态键被禁止**（状态词表存键名，不拼前缀）；
4. **汉字字面量守卫**：产品 .py 字符串字面量（AST，docstring 豁免）不得含汉字；未迁移文件在 `_HAN_WHITELIST` 挂号并注明去处，逐里程碑烧掉，M4 清零（过期条目自动报警）。

## 后果

- 新增用户可见文案的纪律：进 catalog、双侧补齐、调用点字面键——否则四道闸红。
- 菜单「选 项 ▸ 语言」子菜单（✓ 跟随磁盘偏好，auto 一等选项）；写径 `make_set_language` → `update_mp` → `_apply_language`（启动/菜单写径/UI 保存回调三处汇聚）。
- UI 保存整对象 round-trip，`language` 键不会丢；GET 侧 merge 补缺省。
- 语言线程模型：模块级只读快照，GIL 下原子换，菜单线程/config server 线程各读各的，无锁。

## 路线图（里程碑 → PR）

- **M0+M1（本批）**：i18n 管线 + 守卫 + 菜单栏/通知/启动弹窗/关于 双语。
- **M2**：设置窗 HTML 提键（`data-i18n` + JS `t()`）、serve 时注入 `window.__I18N__`、系统页切换 UI、登录页。
- **M3（可选）**：校验器结构化错误（validate.py/mjs + 镜像测试改比码 + 存量文案测试重写）。
- **M4**：长尾表面（ssh_launch 失败分类、ca_trust 引导窗、log_window、SERVICE_CARDS、balance_usage、provider 品牌名、agent_instructions、docker 引导）+ 白名单清零。
- **M5**：en 术语表统一打磨、英文 README（独立轨道）、发版。
