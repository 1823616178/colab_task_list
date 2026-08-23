# 01 登录认证
# kagglehub 默认会读取 ~/.kaggle/kaggle.json 或环境变量,
# 也可以用 login() 交互式登录 / 用代码传入凭据
import kagglehub

# 方式一: 交互式登录(会提示输入 username 和 key)
# kagglehub.login()

# 方式二: 环境变量已配置时无需任何操作, 直接验证身份
print(kagglehub.whoami())  # 打印当前登录用户信息
