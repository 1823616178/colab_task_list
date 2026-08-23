"""colab_transport 共享常量（自 run.py 迁入，值不变）。"""

COLAB_EXEC_MAX_ATTEMPTS = 3
COLAB_OUTPUT_TAIL_CHARS = 200_000
DOWNLOAD_MAX_ATTEMPTS = 5
DOWNLOAD_RETRY_DELAY_SECONDS = 3
REMOTE_OUTPUT_ARCHIVE = "/content/output.zip"
REMOTE_TRANSFER_DIR = "/content/output_transfer"
OUTPUT_STREAM_PREFIX = "__COLAB_OUTPUT_V1__"
REMOTE_TRANSFER_ACK = f"{REMOTE_TRANSFER_DIR}/download.ack"
REMOTE_TRANSFER_ACTIVE = f"{REMOTE_TRANSFER_DIR}/download.active"
REMOTE_TRANSFER_STREAM_REQUEST = f"{REMOTE_TRANSFER_DIR}/stream.request"

# 远端→本地的结构化事件前缀（remote_workflow.py 的非交互输出协议）：
#   __COLAB_OUTPUT_V1__PROGRESS:{json}  → workflow.exec progress 事件
#   __COLAB_OUTPUT_V1__METRICS:{json}   → metrics.sample 事件
# 载荷中都带 "text" 字段，CLI 模式原样打印，保证输出逐字一致。
STREAM_PAYLOAD_PROGRESS = "PROGRESS:"
STREAM_PAYLOAD_METRICS = "METRICS:"
