#!/bin/bash
# view-captures.sh — 方便查看 Magic Proxy 抓包模式落盘的 AI 请求 JSONL
# (~/.magic-proxy-captures/<YYYY-MM-DD>.jsonl,每行一条 chat 补全记录)。
#
# 依赖:jq(macOS 自带)。用法:
#   tools/view-captures.sh [子命令] [选项]
#
# 子命令(默认 summary):
#   summary | ls   一行一条摘要表:时间/厂商/模型/状态/tokens/耗时
#   full | cat     完整美化 JSON,彩色分页(less)
#   msg            只看请求里的 system / user 提问
#   reply          只看助手回复正文(response.reassembled)
#   stats          按厂商+模型统计次数、平均耗时、总 tokens
#   files          列出所有可用的抓包日期文件
#
# 选项:
#   -d, --date YYYY-MM-DD   指定日期(默认今天;今天没有则回退到最新一天)
#   -f, --file PATH         直接指定 jsonl 文件(优先于 --date)
#   -p, --provider NAME     只看某厂商(openai/anthropic/deepseek/doubao/qwen/minimax)
#   -e, --errors            只看 status_code >= 400 的请求
#   -h, --help              显示本帮助
#
# 抓包目录默认 ~/.magic-proxy-captures,可用环境变量 MAGIC_PROXY_CAPTURE_DIR 覆盖。
set -euo pipefail

CAPTURE_DIR="${MAGIC_PROXY_CAPTURE_DIR:-$HOME/.magic-proxy-captures}"

cmd="summary"
date_arg=""
file_arg=""
provider=""
errors_only=0

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# 第一个非选项参数当子命令
if [[ "${1:-}" =~ ^(summary|ls|full|cat|msg|reply|stats|files)$ ]]; then
  cmd="$1"; shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--date)     date_arg="${2:-}"; shift 2 ;;
    -f|--file)     file_arg="${2:-}"; shift 2 ;;
    -p|--provider) provider="${2:-}"; shift 2 ;;
    -e|--errors)   errors_only=1; shift ;;
    -h|--help)     usage 0 ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done

command -v jq >/dev/null 2>&1 || { echo "需要 jq(macOS 自带,或 brew install jq)" >&2; exit 1; }

# ── files 子命令:列出可用日期,无需选定单文件 ──
if [[ "$cmd" == "files" ]]; then
  shopt -s nullglob
  found=("$CAPTURE_DIR"/*.jsonl)
  if [[ ${#found[@]} -eq 0 ]]; then
    echo "抓包目录暂无 jsonl 文件:$CAPTURE_DIR" >&2; exit 0
  fi
  for f in "${found[@]}"; do
    printf "%-14s %6s 条  %s\n" "$(basename "$f" .jsonl)" "$(wc -l < "$f" | tr -d ' ')" "$f"
  done | sort
  exit 0
fi

# ── 选定目标文件 ──
if [[ -n "$file_arg" ]]; then
  FILE="$file_arg"
else
  d="${date_arg:-$(date +%Y-%m-%d)}"
  FILE="$CAPTURE_DIR/$d.jsonl"
  if [[ ! -f "$FILE" ]]; then
    # 今天/指定日期无文件 → 回退到最新一天
    shopt -s nullglob
    latest=$(ls -1 "$CAPTURE_DIR"/*.jsonl 2>/dev/null | sort | tail -1 || true)
    if [[ -z "$latest" ]]; then
      echo "抓包目录暂无 jsonl 文件:$CAPTURE_DIR" >&2; exit 0
    fi
    echo "（$d 无记录,改看最新:$(basename "$latest")）" >&2
    FILE="$latest"
  fi
fi

[[ -f "$FILE" ]] || { echo "文件不存在:$FILE" >&2; exit 1; }

# ── 前置 jq 过滤(厂商 / 仅错误),再交给各子命令 ──
sel='.'
[[ -n "$provider" ]] && sel="$sel | select(.provider==\"$provider\")"
[[ $errors_only -eq 1 ]] && sel="$sel | select(.status_code >= 400)"

# 各厂商 token 键名不一,统一取:优先 openai 系,再 anthropic 系
tokjq='((.usage.total_tokens) // ((.usage.input_tokens // 0) + (.usage.output_tokens // 0)) // (.usage.prompt_tokens // 0) + (.usage.completion_tokens // 0)) // 0'

case "$cmd" in
  summary|ls)
    { printf "时间\t厂商\t模型\t状态\ttokens\t耗时\n";
      jq -r "$sel | [(.ts|sub(\"T\";\" \")|sub(\"\\\\..*Z\";\"\")), .provider, .model, (.status_code|tostring), ($tokjq|tostring), \"\(.duration_ms)ms\"] | @tsv" "$FILE"
    } | column -t -s $'\t'
    ;;
  full|cat)
    jq -C "$sel" "$FILE" | less -R
    ;;
  msg)
    jq -r "$sel | \"── \(.ts) [\(.provider)/\(.model)] ──\", (.request.system // empty | \"system: \\(.)\"), (.request.messages[]? | \"\(.role): \(.content)\"), \"\"" "$FILE" | less -R
    ;;
  reply)
    jq -r "$sel | \"── \(.ts) [\(.provider)/\(.model)] status=\(.status_code) ──\", (.response.reasoning // empty | \"[reasoning] \\(.)\"), (.response.reassembled // \"(空)\"), \"\"" "$FILE" | less -R
    ;;
  stats)
    echo "文件:$FILE"
    echo
    jq -s "
      map($sel) |
      group_by(.provider + \"/\" + (.model // \"?\")) |
      map({
        k: (.[0].provider + \"/\" + (.[0].model // \"?\")),
        n: length,
        avg: ((map(.duration_ms) | add) / length | floor),
        tok: (map($tokjq) | add)
      }) |
      (.[] | \"\(.k)\t次数=\(.n)\t平均=\(.avg)ms\t总tokens=\(.tok)\")
    " "$FILE" -r | column -t -s $'\t'
    ;;
esac
