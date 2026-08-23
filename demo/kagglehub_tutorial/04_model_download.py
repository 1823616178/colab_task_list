# 04 下载模型权重
import kagglehub

# 句柄格式: <owner>/<model>/<framework>/<variation>
# 也可加 /<version> 指定版本
path = kagglehub.model_download("google/gemma/pyTorch/2b")
print("模型路径:", path)

# 下载模型中的单个文件(比如只要 tokenizer)
# tok_path = kagglehub.model_download("google/gemma/pyTorch/2b", path="tokenizer.model")
# print("tokenizer 路径:", tok_path)

# 之后即可用对应框架加载, 例如 PyTorch:
# import torch
# state_dict = torch.load(os.path.join(path, "gemma-2b.ckpt"))
