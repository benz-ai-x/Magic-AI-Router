# ADR-008: 架构评审收尾——已裁决不做清单与触发条件

- 状态：Accepted
- 日期：2026-09-18
- 决策者：tech-lead（用户确认「按建议执行」）
- 影响范围：无代码影响（本文是评审裁决记录）
- 关联：四轮 `/improve-codebase-architecture` 评审（R1–R4）；ADR-007 及其收敛注记

## 上下文

2026-09-18 连续四轮架构评审，落地 8 个深化候选（提交 4f031a3 / a1f809a /
3607a0d / ee6b093）：SshSession 收敛（会话三件套镜像 3→1）、prepare 校验
分域（303→约80行 orchestrator）、probe_port 还债、ForwardState/MountState
NamedTuple 化、次级清偿包六件（含 reload_cfg 处置分叉的行为修复）、
RuntimeProjection 单一 seam（三参穿三层→一）、抓包「实际在跑」语义归域
（CaptureController.actively_running）、docker 默认配置内容单一归宿
（default_config_yaml）。另修正一项误报（save_config 并非孤儿——
load_config 迁移路径在用）。

R4 复核后确认：**无「必须修」的硬伤**。剩余候选边际收益递减，本文记录
它们的裁决与「何时才做」的触发条件，防止未来评审（人或 AI）重复提议。

## 决策：暂不做清单与触发条件

| 候选 | 裁决 | 触发条件（满足才重开） |
|---|---|---|
| 凭据解析单一归宿（4 个 get 触点 × 3 种入参形状） | 暂不做 | 下一个动凭据的功能（新认证方式/多凭据槽）开工前 |
| app.py 桥接分发注册化（5 分支 if/elif） | 暂不做 | 第 6 个 ACTION_* 出现，或 app.py 再增 50 行 |
| config_ui.html LAYER2 构建期拆分（1134 行无提取路径） | 暂不做 | 下一个大视图，或 L2 再 +200 行；前提是团队接受「构建产物不可手改」 |
| 代理会话迁移 SshSession | **不做** | 除非暂停/降级语义要重做——27 处三件套引用缠绕 paused/restart 降级/_launched_proxy_id，是策略性内联而非意外重复（强迁把策略压进通用 interface 反而变浅）；ADR-007 收敛注记已声明此边界 |
| chromium_proxy 包归属（住 capture 域） | 随手活 | 任何动 capture 包结构的 PR 顺带 |
| UI 文案双轨（CaptureController 域内拼文案 vs menu_builder） | 随手活 | 同上，不专程开工 |

## 理由

- **YAGNI**：前三项的摩擦要么边缘触发（凭据分叉只在手编配置显形），要么
  复利点未到（注册表要等第 6 个动作才有杠杆）。
- **deletion test 不通过的不做**：代理会话的内联与 SshSession 不同构
  （代理会话有 -D、暂停、降级、角色解析），删除内联不会让复杂度集中，
  只会把它搬进一堆布尔参数里。
- 误报教训入档：评审结论须核实生产调用面再判孤儿（save_config 案例）。

## 后果

- 未来评审提议上述候选时，先对照本文触发条件；未满足即引用本文关闭。
- 触发条件满足时，按各轮报告卡片的方案起步（R4 报告存于会话临时目录，
  方案要点已内化到本文表格与关联 ADR）。
- 本文档随裁决变化更新（某项落地后移入「已落地」并注明提交号）。
