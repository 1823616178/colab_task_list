# 03 直接加载数据集为 pandas DataFrame / HF Dataset / polars 等
# 无需手动处理路径和解压
import kagglehub
from kagglehub import KaggleDatasetAdapter

# 加载为 pandas DataFrame(需指定数据集内的文件名)
df = kagglehub.load_dataset(
    KaggleDatasetAdapter.PANDAS,
    "uciml/iris",
    "Iris.csv",
)
print(df.head())
print(df.shape)

# 其他可用的 Adapter:
# KaggleDatasetAdapter.HUGGING_FACE  -> datasets.Dataset
# KaggleDatasetAdapter.POLARS        -> polars.DataFrame(需指定 SQL 查询)
