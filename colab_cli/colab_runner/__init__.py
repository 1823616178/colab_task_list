"""colab_runner：Colab 编排器的常驻作业服务（doc/01 §2，P1）。

FastAPI + uvicorn 单 worker，绑定 127.0.0.1:8765；不碰 MySQL（doc/03 §1
铁律），只在本地维护 runner_state.json 用于自身崩溃恢复。
"""

__version__ = "0.1.0"
