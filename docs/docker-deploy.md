# Docker 网关部署（Linux / 无 GUI）

issue #22：把 Suanpan AI 路由网关以 Docker 容器形态部署到 Linux 服务器。
只含网关——不带 Magic Proxy 隧道、TLS 抓包或任何 macOS GUI 能力；macOS
侧行为零变化（本特性不修改任何既有文件）。

## 快速开始

```bash
# 1. 构建并启动（首启自动生成 docker/data/suanpan.yaml 默认配置）
bash docker/suanpan.sh up

# 2. 编辑配置，填入你的 Provider（也可直接编辑 docker/data/suanpan.yaml）
vim docker/data/suanpan.yaml

# 3. 重启使配置生效
bash docker/suanpan.sh down && bash docker/suanpan.sh up

# 4. 健康检查
curl http://127.0.0.1:9527/health

# 5. 对接本机 Claude Code
bash docker/suanpan.sh sync
```

## 命令一览（docker/suanpan.sh）

| 命令 | 作用 |
|---|---|
| `up` | 构建镜像并启动容器（后台） |
| `down` | 停止并移除容器（`./data` 卷保留） |
| `status` | 容器运行状态 |
| `logs` | 跟随网关日志 |
| `sync [--dry-run]` | 同步宿主机 `~/.claude/settings.json` 指向本网关 |

## 配置

配置文件在 **`docker/data/suanpan.yaml`**（bind mount 到容器 `/data`）。
首启自动生成最小默认配置；字段语义与 macOS 版完全一致，参考
`docs/examples/suanpan.example.yaml`。要点：

- **Provider API key**：写 `api_key`（明文进 YAML，注意文件权限）或
  `api_key_env`（引用容器环境变量——compose 里 `environment:` 注入，
  避免 key 落盘）。
- **用量日志**：默认配置已把 `usage_log.path` 指到 `/data/logs/usage.jsonl`
  ——落在 `docker/data/` 卷里，`down`/重建容器不丢。
- **本地客户端 token**：`sync` 首次运行会在 `docker/data/magic-proxy.json`
  生成 `local_client_token`（随机 32 hex）。它与 `~/.claude/settings.json`
  里的 `ANTHROPIC_AUTH_TOKEN` 同源同值，同样跨容器重建稳定。

## Claude Code 对接（sync）

`bash docker/suanpan.sh sync` 在**宿主机** `~/.claude/settings.json` 写入：

- `ANTHROPIC_BASE_URL=http://127.0.0.1:9527`
- `ANTHROPIC_AUTH_TOKEN=<本地 token>`（见上；明文不回显于输出）
- `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`
- 由路由规则推导的模型角色映射（含 `[1M]` 上下文语义）

行为与 macOS 版完全一致（同一实现 `services/claude_code_setup`）：
幂等（已指向本网关则不写入）、首写自动备份 `settings.json.bak`、替换
已设 `AUTH_TOKEN` 时明文不回显。`sync --dry-run` 只打印逐键 diff 不落盘。

### 若启用了网关 api_key

默认配置**不设**顶层 `api_key`（网关不校验客户端身份，信任边界=宿主机
回环端口映射）。若你在 `suanpan.yaml` 设了顶层 `api_key`，其值必须等于
`docker/data/magic-proxy.json` 的 `local_client_token`——否则 sync 写入的
`AUTH_TOKEN` 会被网关 401。两值对齐或保持 `api_key` 为空，二选一。

## 两条必须知道的边界

1. **宿主机映射端口必须等于容器 `listen_port`**。compose 固定
   `127.0.0.1:9527:9527`；若改了 `suanpan.yaml` 的 `listen_port`，compose
   的端口映射（和 `sync` 写入的 BASE_URL 端口）须同步改。映射永远绑定
   `127.0.0.1`——不要改成对外网卡，容器内监听 `0.0.0.0` 是 Docker 标准
   姿势，唯一的访问控制就在宿主机侧这个回环绑定上。
2. **`~/.claude` 是读写挂载**。`sync` 直接改宿主机的
   `~/.claude/settings.json`（容器内路径 `/host-claude/settings.json`）。
   自托管共享服务器上这意味着"能操作该容器的人能改你的 Claude Code
   配置"。谨慎场景先 `sync --dry-run` 看逐键 diff 再实跑；首次写入会
   自动备份 `settings.json.bak`。

## Linux 兼容（Security stub）

`suanpan.config` → `mpconf.config` → `sysctl.keychain` 的模块级 import 链
最终会 `import Security`（PyObjC，仅 macOS）。容器内没有这个模块——
`docker/entry.py` 在 import suanpan 前向 `sys.modules` 注入带
`__docker_stub__` 标记的空模块。Docker 路径永不调用被 stub 的 keychain
函数（SSH 隧道密码存取是 macOS 菜单栏版的能力，容器版不带）。这也是
镜像里出现 `sysctl/`、`capture/` 目录的原因：`mpconf.config` 模块级引用
了它们的名字。

## 镜像内容

`python:3.12-slim` + `requirements-suanpan.txt`（fastapi / uvicorn /
httpx / pyyaml / pydantic / structlog / h2）+ `suanpan/`、`mpconf/`、
`services/`、`sysctl/`、`capture/`、`docker/entry.py`。不含
rumps / pyobjc / mitmproxy / pyinstaller 等 macOS 专属依赖。仅本地构建，
不发布 registry。

## 非目标

- Magic Proxy 隧道、TLS 抓包、GUI
- systemd / 自带 daemon 管理（守护/重启交给容器运行时 `restart: unless-stopped`）
- 用量统计 / 余额查询命令（macOS 菜单能力不对齐，后续按需）
