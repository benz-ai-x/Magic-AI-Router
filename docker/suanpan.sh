#!/usr/bin/env bash
# Suanpan 网关宿主机一键脚本（issue #22）。
#
# 用法：bash docker/suanpan.sh <up|down|status|logs|sync [--dry-run]>
#
# up     构建并启动网关（首启自动生成 ./data/suanpan.yaml 默认配置）
# down   停止并移除容器（./data 卷保留——配置与用量日志不丢）
# status 容器运行状态
# logs   跟随网关日志
# sync   把宿主机 ~/.claude/settings.json 同步指向本网关
#        --dry-run 只打印逐键 diff，不落盘
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$script_dir"

cmd="${1:-help}"
shift || true

case "$cmd" in
  up)
    docker compose up -d --build
    echo "网关已启动：http://127.0.0.1:9527（健康检查 /health）"
    echo "对接 Claude Code：bash docker/suanpan.sh sync"
    ;;
  down)
    docker compose down
    ;;
  status)
    docker compose ps
    ;;
  logs)
    docker compose logs -f --tail=100
    ;;
  sync)
    docker compose exec suanpan python3 /app/docker/entry.py sync-claude-code "$@"
    ;;
  config-ui)
    echo "配置页面: http://127.0.0.1:9528/"
    echo "Bearer token:"
    docker compose exec suanpan python3 /app/docker/entry.py config-token
    ;;
  help|*)
    # 从第 2 行起取连续注释行——不硬编码行号，注释改动不漂移
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' \
      "$script_dir/suanpan.sh"
    exit 1
    ;;
esac
