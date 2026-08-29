# 06 下载别人 Notebook 的运行输出
import kagglehub
import os

# 句柄: <owner>/<kernel-slug>
path = kagglehub.notebook_output_download(
    "elifduman/titanic-dataset-with-pandas"
)
print("输出路径:", path)
print("文件列表:", os.listdir(path))
