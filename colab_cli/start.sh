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

choose_accelerator() {
    local default_selection
    local selection

    default_selection="${COLAB_ACCELERATOR_DEFAULT:-2}"
    if [[ ! "$default_selection" =~ ^[1-7]$ ]]; then
        echo "错误: COLAB_ACCELERATOR_DEFAULT 必须是 1-7。" >&2
        exit 1
    fi

    while true; do
        echo
        echo "请选择 Colab 加速器配置:"
        echo "  1) T4    - 免费额度优先，速度较慢"
        echo "  2) L4    - 24GB 显存，速度和费用较均衡"
        echo "  3) A100  - 推荐用于大型视频工作流，需要计算单元"
        echo "  4) G4    - 取决于账号可用额度"
        echo "  5) H100  - 性能最高，需要较多计算单元"
        echo "  6) TPU v5e-1 - 仅适用于支持 PyTorch/XLA 或 JAX 的工作流"
        echo "  7) TPU v6e-1 - 仅适用于支持 PyTorch/XLA 或 JAX 的工作流"
        read -r -p "请输入序号 [$default_selection]: " selection
        selection="${selection:-$default_selection}"

        case "$selection" in
            1) accelerator_type="gpu"; accelerator="T4"; return ;;
            2) accelerator_type="gpu"; accelerator="L4"; return ;;
            3) accelerator_type="gpu"; accelerator="A100"; return ;;
            4) accelerator_type="gpu"; accelerator="G4"; return ;;
            5) accelerator_type="gpu"; accelerator="H100"; return ;;
            6) accelerator_type="tpu"; accelerator="v5e1"; return ;;
            7) accelerator_type="tpu"; accelerator="v6e1"; return ;;
            *) echo "无效选择，请输入 1-7。" >&2 ;;
        esac
    done
}

choose_run_mode() {
    local selection

    while true; do
        echo
        echo "请选择 VM 运行方式:"
        echo "  1) 新建 VM  - 创建新会话并安装/检查 ComfyUI"
        echo "  2) 复用 VM  - 使用现有会话并直接提交工作流"
        read -r -p "请输入序号 [1]: " selection
        selection="${selection:-1}"

        case "$selection" in
            1) run_mode="new"; return ;;
            2) run_mode="reuse"; return ;;
            *) echo "无效选择，请输入 1 或 2。" >&2 ;;
        esac
    done
}

choose_workflow() {
    local configs
    local index
    local selection

    if [[ -n "${COLAB_WORKFLOW_CONFIG_PRESET:-}" ]]; then
        if [[ ! -f "$COLAB_WORKFLOW_CONFIG_PRESET" ]]; then
            echo "错误: 工作流配置不存在: $COLAB_WORKFLOW_CONFIG_PRESET" >&2
            exit 1
        fi
        workflow_config="$COLAB_WORKFLOW_CONFIG_PRESET"
        echo "已选择工作流配置: $(basename -- "$workflow_config")"
        return
    fi

    shopt -s nullglob
    configs=("$SCRIPT_DIR/workflows"/*.json)
    shopt -u nullglob
    if [[ ${#configs[@]} -eq 0 ]]; then
        echo "错误: $SCRIPT_DIR/workflows 中没有工作流配置。" >&2
        exit 1
    fi

    while true; do
        echo
        echo "请选择工作流配置:"
        for index in "${!configs[@]}"; do
            printf '  %d) %s\n' "$((index + 1))" "$(basename -- "${configs[$index]}")"
        done
        read -r -p "请输入序号 [1]: " selection
        selection="${selection:-1}"
        if [[ "$selection" =~ ^[0-9]+$ ]] &&
                ((selection >= 1 && selection <= ${#configs[@]})); then
            workflow_config="${configs[$((selection - 1))]}"
            return
        fi
        echo "无效选择，请重新输入。" >&2
    done
}

ask_yes_no() {
    local prompt="$1"
    local default_answer="$2"
    local answer
    local hint

    if [[ "$default_answer" == "yes" ]]; then
        hint="Y/n"
    else
        hint="y/N"
    fi

    while true; do
        read -r -p "$prompt [$hint]: " answer
        if [[ -z "$answer" ]]; then
            [[ "$default_answer" == "yes" ]]
            return
        fi

        case "${answer,,}" in
            y|yes|是) return 0 ;;
            n|no|否) return 1 ;;
            *) echo "请输入 y 或 n。" >&2 ;;
        esac
    done
}

choose_workflow
choose_run_mode
if [[ "$run_mode" == "new" ]]; then
    choose_accelerator
fi

default_session="${COLAB_SESSION_DEFAULT:-comfyui-h3}"
read -r -p "会话名称 [$default_session]: " session
session="${session:-$default_session}"

if [[ "$run_mode" == "reuse" ]]; then
    drive_prompt="检测并使用已挂载的 Google Drive？不会重新认证"
else
    drive_prompt="挂载 Google Drive 并使用模型缓存？"
fi

if ask_yes_no "$drive_prompt" "yes"; then
    mount_drive="yes"
else
    mount_drive="no"
fi

if [[ "$run_mode" == "reuse" ]]; then
    keep_vm="yes"
    echo "复用模式会在任务结束后继续保留 VM，请使用 stop.sh 手动关闭。"
elif ask_yes_no "任务结束后保留 Colab VM？" "no"; then
    keep_vm="yes"
else
    keep_vm="no"
fi

while true; do
    read -r -p "执行超时秒数，0 表示无限制 [86400]: " timeout
    timeout="${timeout:-86400}"
    if [[ "$timeout" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        break
    fi
    echo "超时必须是大于等于 0 的数字。" >&2
done

echo
echo "本次启动配置:"
echo "  工作流:       $(basename -- "$workflow_config")"
echo "  VM 模式:      $run_mode"
if [[ "$run_mode" == "new" ]]; then
    echo "  加速器:       ${accelerator_type^^} $accelerator"
else
    echo "  加速器:       使用现有会话配置"
fi
echo "  会话:         $session"
if [[ "$run_mode" == "reuse" && "$mount_drive" == "yes" ]]; then
    echo "  Drive 缓存:   仅使用已有挂载（不认证）"
else
    echo "  Drive 缓存:   $mount_drive"
fi
echo "  保留 VM:      $keep_vm"
echo "  超时:         $timeout 秒"
echo

if ! ask_yes_no "确认启动？" "no"; then
    echo "已取消。"
    exit 0
fi

run_args=(
    "$SCRIPT_DIR/run.py"
    --workflow-config "$workflow_config"
    --session "$session"
    --timeout "$timeout"
)

if [[ "$run_mode" == "new" ]]; then
    if [[ "$accelerator_type" == "tpu" ]]; then
        run_args+=(--tpu "$accelerator")
    else
        run_args+=(--gpu "$accelerator")
    fi
else
    run_args+=(--reuse)
fi

if [[ "$mount_drive" == "no" ]]; then
    run_args+=(--no-mount-drive)
fi

if [[ "$keep_vm" == "yes" ]]; then
    run_args+=(--keep)
fi

exec "$python_cmd" "${run_args[@]}"
