# 05 下载竞赛数据
import kagglehub
import os

# 句柄就是竞赛 slug(URL 最后一段)
path = kagglehub.competition_download("titanic")
print("竞赛数据路径:", path)
print("文件列表:", os.listdir(path))

# 只下载单个文件
train_path = kagglehub.competition_download("titanic", path="train.csv")
print("train.csv 路径:", train_path)
