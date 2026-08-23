#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
    python_cmd="python3"
elif command -v python >/dev/null 2>&1; then
    python_cmd="python"
else
    echo "错误: 找不到 python3 或 python。" >&2
    exit 1
fi

if ! command -v colab >/dev/null 2>&1; then
    echo "错误: 找不到 colab 命令，请先安装 google-colab-cli。" >&2
    exit 1
fi

confirm_stop() {
    local prompt="$1"
    local answer

    while true; do
        read -r -p "$prompt [y/N]: " answer
        case "${answer,,}" in
            y|yes|是) return 0 ;;
            n|no|否|"") return 1 ;;
            *) echo "请输入 y 或 n。" >&2 ;;
        esac
    done
}

stop_session() {
    local session_name="$1"
    local endpoint="$2"

    if [[ "$session_name" == "?" ]]; then
        echo ">>> 正在停止未映射 Runtime: $endpoint"
        "$python_cmd" "$SCRIPT_DIR/run.py" --stop-endpoint "$endpoint"
    else
        echo ">>> 正在停止命名会话: $session_name"
        "$python_cmd" "$SCRIPT_DIR/run.py" --session "$session_name" --stop
    fi
}

echo ">>> 正在查询运行中的 Colab 会话..."
if ! sessions_output="$(NO_COLOR=1 TERM=dumb colab sessions 2>&1)"; then
    echo "$sessions_output" >&2
    echo "错误: 无法读取 Colab 会话列表。" >&2
    exit 1
fi

session_names=()
session_endpoints=()
session_labels=()
while IFS=$'\t' read -r session_name endpoint hardware variant; do
    [[ -z "$endpoint" ]] && continue
    session_names+=("$session_name")
    session_endpoints+=("$endpoint")
    if [[ "$session_name" == "?" ]]; then
        session_labels+=(
            "未映射会话 | Runtime: $endpoint | Hardware: $hardware | Variant: $variant"
        )
    else
        session_labels+=(
            "$session_name | Runtime: $endpoint | Hardware: $hardware | Variant: $variant"
        )
    fi
done < <(
    printf '%s\n' "$sessions_output" |
        sed -nE \
            's/^\[([^]]+)\] ([^ ]+) \| Hardware: ([^|]+) \| Variant: (.*)$/\1\t\2\t\3\t\4/p'
)

if [[ ${#session_names[@]} -eq 0 ]]; then
    if [[ -n "$sessions_output" ]]; then
        printf '%s\n' "$sessions_output"
    fi
    echo "当前没有检测到正在运行的 Colab 会话。"
    exit 0
fi

echo
echo "正在运行的 Colab 会话："
for index in "${!session_names[@]}"; do
    printf '  %d) %s\n' "$((index + 1))" "${session_labels[$index]}"
done
echo "  a) 停止全部会话"
echo "  0) 取消"

while true; do
    read -r -p "请选择要停止的会话 [0]: " selection
    selection="${selection:-0}"

    case "${selection,,}" in
        0|q|quit|取消)
            echo "已取消。"
            exit 0
            ;;
        a|all|全部)
            if ! confirm_stop "确认停止全部 ${#session_names[@]} 个会话？"; then
                echo "已取消。"
                exit 0
            fi

            failed_sessions=()
            for index in "${!session_names[@]}"; do
                if ! stop_session \
                        "${session_names[$index]}" \
                        "${session_endpoints[$index]}"; then
                    failed_sessions+=("${session_labels[$index]}")
                fi
            done

            if [[ ${#failed_sessions[@]} -gt 0 ]]; then
                echo "以下会话停止失败：${failed_sessions[*]}" >&2
                exit 1
            fi
            echo ">>> 全部会话已停止。"
            exit 0
            ;;
    esac

    if [[ "$selection" =~ ^[0-9]+$ ]] &&
            ((selection >= 1 && selection <= ${#session_names[@]})); then
        index=$((selection - 1))
        session_name="${session_names[$index]}"
        endpoint="${session_endpoints[$index]}"
        label="${session_labels[$index]}"
        if ! confirm_stop "确认停止 '$label'？"; then
            echo "已取消。"
            exit 0
        fi
        if ! stop_session "$session_name" "$endpoint"; then
            echo "错误: 停止失败: $label" >&2
            exit 1
        fi
        echo ">>> 已确认停止: $label"
        exit 0
    else
        echo "无效选择，请输入会话序号、a 或 0。" >&2
    fi
done
