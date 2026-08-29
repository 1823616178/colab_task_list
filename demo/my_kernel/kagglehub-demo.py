# kagglehub 使用演示: 在 Kaggle 云端下载数据集并预览
import os

import kagglehub
from kagglehub import KaggleDatasetAdapter

print("当前用户:", kagglehub.whoami())

# 下载整个数据集(返回缓存路径, 自动解压)
path = kagglehub.dataset_download("uciml/iris")
print("数据集路径:", path)
print("文件列表:", os.listdir(path))

# 直接加载为 pandas DataFrame
df = kagglehub.load_dataset(
    KaggleDatasetAdapter.PANDAS,
    "uciml/iris",
    "Iris.csv",
)
print(df.head())
print("形状:", df.shape)
