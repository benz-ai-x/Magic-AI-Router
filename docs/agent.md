# Magic AI Router

> macOS menu bar app. Two products in one: SSH tunnel HTTP→SOCKS5 proxy + AI routing gateway.

## What it does

1. **Proxy Tunnel** — SSH 隧道：通过 SSH 连接到远程服务器，在本地起 HTTP 代理（默认 :8888），将流量经 SOCKS5 转发到远端。支持多隧道、密钥/密码认证（密码走 macOS Keychain）。
2. **AI 路由** — 算盘网关：在本地 :9527 起一个 Anthropic Messages API 兼容的网关，将请求按规则路由到多个 LLM 后端（GLM、DeepSeek、KIMI、QWEN、Anthropic 等）。支持流式 SSE、缓存 token 统计、用量日志。

## Config files

### ~/.magic-proxy.json — Proxy Tunnel 配置

```json
{
  "socks5_port": 1080,
  "http_listen_port": 8888,
  "system_proxy_default": false,
  "current_tunnel": 0,
  "tunnels": [
    {
      "name": "my-server",
      "ssh_user": "root",
      "ssh_host": "1.2.3.4",
      "ssh_port": 22,
      "auth_type": "key",
      "ssh_key": "~/.ssh/id_rsa",
      "ssh_compression": true
    }
  ],
  "capture_port": 8080,
  "capture_dir": "~/.magic-proxy-captures",
  "retention_days": 7,
  "prevent_sleep": false,
  "launch_at_login": false,
  "config_port": 9528
}
```

- `auth_type`: `"key"`（默认，用 ssh_key）或 `"password"`（需 sshpass，密码走 Keychain）
- `http_listen_port`: 本地 HTTP 代理监听端口（整型；旧 `"host:port"` 字符串 `http_listen` 读时兼容）
- `current_tunnel`: 当前使用的隧道索引

### ~/.suanpan.yaml — AI 路由配置

```yaml
listen_port: 9527        # 整型；旧 "host:port" 字符串 listen 读时兼容
api_key: null              # null = 不校验客户端 key
request_timeout_s: 3600
body_limit_mb: 50

usage_log:
  enabled: true
  path: ~/.suanpan/logs/usage.jsonl

providers:
  GLM_MAX:
    base_url: https://open.bigmodel.cn/api/anthropic
    api_key: "your-api-key"
    api_key_env: null       # 可选：从环境变量读取
    auth_header: x-api-key  # 或 Authorization（Bearer）
    enabled: true
    models:
      - glm-5.2

router:
  default: GLM_MAX/glm-5.2  # 未命中规则时的兜底

rules:
  - match_prefix: claude-opus
    route_to: GLM_MAX/glm-5.2
  - match_prefix: claude-sonnet
    route_to: DeepSeek/deepseek-v4-pro
```

**路由优先级**（命中即停止）：
1. **内联覆盖** — model 含 `供应商/模型`（如 `KIMI/k3`）直接打给该供应商
2. **前缀规则** — `rules` 逐条匹配 model 名前缀
3. **默认路由** — `router.default`

## REST API（:9528，需 token）

所有端点需要 bearer token（issue #10 后 URL 永不带凭证）：
- **AI agent / curl**：从 `Authorization: Bearer TOKEN` header 传入。token 获取：macOS 菜单栏「复制 AI 助手指令」；Docker 无菜单——`bash docker/suanpan.sh config-ui` 打印。
- **设置窗（WKWebView）**：首次打开经桥接带 Authorization 头导航，响应种下 `cfgsess` HttpOnly SameSite=Strict 会话 cookie——后续请求由 cookie 承载，JS 从不接触 token。

query-string 认证已删除；无凭证时 `/api/*` 返回 401 JSON，裸 GET `/` 返回登录页（浏览器输入 token 即可进入管理面板）。

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/state` | 读取全部配置（mp + sp），密码/密钥已掩码 |
| PUT | `/api/state` | 保存全部配置（body: `{"mp": {...}, "sp": {...}}`） |
| GET | `/api/balance` | 查询各供应商余额/配额；套餐类含 5小时/每周/每月 配额窗口，每月行可能是网关本地聚合（`source: "local"`，UI 标注「（网关）」） |
| GET | `/api/usage?range=today\|7d\|month\|all` | 聚合本地用量日志；缺省 `all`，`month` = CST 自然月；返回总览、供应商、CST 每日与路由来源统计 |
| POST | `/api/fetch-models` | 拉取供应商模型列表（body: `{"provider": "GLM_MAX"}`） |
| POST | `/api/test-provider` | 测试供应商连通性（body: `{"provider": "GLM_MAX", "model": "glm-5.2"}`） |

### 示例：读取当前配置

```bash
curl -H "Authorization: Bearer TOKEN" http://127.0.0.1:9528/api/state
```

### 示例：添加 AI 供应商并设路由

1. 读取当前配置 → 修改 `sp.providers` 和 `sp.rules` → PUT 回去
2. 用 `/api/test-provider` 测试连通性
3. 网关会自动热重载配置

## Common setup scenarios

### 场景 1：配置 SSH 隧道代理

1. 确认 `~/.magic-proxy.json` 有至少一条 tunnel
2. 密钥认证：设 `auth_type: "key"`, `ssh_key: "~/.ssh/id_rsa"`
3. 密码认证：设 `auth_type: "password"`，密码会存入 Keychain
4. 从菜单栏「代理隧道 ▸ 连接代理」启动
5. 系统代理：菜单栏「代理隧道 ▸ 系统代理：开」

### 场景 2：配置 AI 路由让 Claude Code 使用

1. 编辑 `~/.suanpan.yaml`，添加供应商（base_url + api_key + auth_header）
2. 设置 `router.default` 为 `供应商/模型`
3. 可选：添加 `rules` 按模型名前缀分流
4. 从菜单栏「AI 路由 ▸ 启动路由」启动网关（:9527）
5. Claude Code 设置环境变量：
   ```bash
   export ANTHROPIC_BASE_URL=http://127.0.0.1:9527
   ```
6. 现在 Claude Code 的请求会经网关路由到你配置的后端

### 场景 3：CC Switch / 第三方客户端

- 如果客户端的 model 设为 `claude-xxx`：走前缀规则
- 如果设为 `供应商/模型`（如 `KIMI/k3`）：直接路由到该供应商（内联覆盖，跳过规则）
- 网关不校验 api_key（除非 `~/.suanpan.yaml` 里设了 `api_key`）

## Troubleshooting

- **网关没启动**：检查菜单栏「AI 路由 ▸ 启动路由」；或查看 `~/Library/Logs/MagicProxy.log`
- **502 错误**：检查供应商 base_url / api_key 是否正确；用 `/api/test-provider` 测试
- **SSH 连接失败**：检查密钥权限 `chmod 600 ~/.ssh/id_rsa`；密码认证需 `brew install sshpass`
- **端口被占用**：菜单栏会自动清理 :9527/:9528，或手动 `lsof -i :9527`
- **余额/token 不显示**：不同供应商的 SSE 格式有差异，网关已做 max-merge 兼容；如仍有问题查看 `~/.suanpan/logs/usage.jsonl`

## Menu structure

菜单栏（从上到下）：
- 状态行（绿/黄/灰 + 隧道名 + 流量）
- 代理隧道 ▸（连接/暂停/重新连接 · 系统代理 · 隧道选择 · 经代理启动 App）
- AI 路由 ▸（启动/停止/重启 · 重新加载 · 复制地址）
- 抓包 ▸（TLS MITM 抓包，需信任 CA）
- 偏好设置…（打开 Web 配置面板 :9528）
- 查看日志 · 防睡眠 · 登录启动 · 关于 · 退出
