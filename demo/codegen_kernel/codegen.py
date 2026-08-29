# 用 Kaggle 赠送的 GPU 额度跑 Gemma 模型生成代码
# 生成结果写入 /kaggle/working/, 之后用 kaggle kernels output 拉回本地
import os

os.environ["KERAS_BACKEND"] = "jax"  # Gemma 在 Kaggle 上推荐用 JAX 后端

import keras_hub

# 1. 加载模型(权重来自 Kaggle Models, 需先在网页上接受 Gemma 许可协议)
#    https://www.kaggle.com/models/google/gemma -> 点 "Accept"
model = keras_hub.models.GemmaCausalLM.from_preset("gemma3_1b")

# 2. 构造 prompt: 描述你要生成的代码
PROMPT = """<start_of_turn>user
Write a Python function `quick_sort(arr)` that implements quicksort,
with a docstring and type hints. Output ONLY the code, no explanation.
<end_of_turn>
<start_of_turn>model
"""

# 3. 生成
output = model.generate(PROMPT, max_length=512)
print("=== 模型原始输出 ===")
print(output)

# 4. 提取代码部分并保存(去掉 prompt 前缀)
generated = output.split("<start_of_turn>model")[-1].strip()
with open("/kaggle/working/generated_code.py", "w") as f:
    f.write(generated + "\n")

print("=== 已保存到 /kaggle/working/generated_code.py ===")
