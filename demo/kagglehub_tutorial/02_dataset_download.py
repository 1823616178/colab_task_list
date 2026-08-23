# 02 下载数据集
import kagglehub
import os

# 下载整个数据集, 返回本地缓存路径(自动解压)
path = kagglehub.dataset_download("uciml/iris")
print("数据集路径:", path)
print("文件列表:", os.listdir(path))

# 只下载数据集中的单个文件
csv_path = kagglehub.dataset_download("uciml/iris", path="Iris.csv")
print("单个文件路径:", csv_path)
