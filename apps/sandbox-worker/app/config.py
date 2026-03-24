import os

INNER_HTTP_SECRET = os.getenv("INNER_HTTP_SECRET", "")
SKILLS_DIR = os.getenv("SKILLS_DIR", "/sandbox/skills")
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT", "120"))
MAX_MEMORY_MB = int(os.getenv("MAX_MEMORY_MB", "256"))
MAX_NPROC = int(os.getenv("MAX_NPROC", "256"))
MAX_FSIZE_MB = int(os.getenv("MAX_FSIZE_MB", "10"))

# 允许在沙箱中使用的网络命令，逗号分隔（如 "curl,wget"）
# 默认空 = 全部封锁；设为 "*" = 全部放行
ALLOWED_NETWORK_COMMANDS: str = os.getenv("ALLOWED_NETWORK_COMMANDS", "")
