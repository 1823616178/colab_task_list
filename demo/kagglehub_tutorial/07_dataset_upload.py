# 07 上传数据集(写操作, 谨慎运行)
# 会创建或更新你自己账号下的数据集
import kagglehub

# 句柄: <你的用户名>/<数据集slug>, 本地目录里放要上传的文件
handle = "laycatbot/my-first-dataset"      # 改成你的用户名和数据集名
local_dir = "./my_dataset_folder"           # 改成实际目录

# 新建数据集(目录下需有文件; 可加 version_notes 参数写版本说明)
kagglehub.dataset_upload(handle, local_dir)

# 更新已有数据集(会自动生成新版本)
# kagglehub.dataset_upload(handle, local_dir, version_notes="add more rows")
