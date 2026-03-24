import os

INNER_HTTP_SECRET = os.getenv("INNER_HTTP_SECRET", "")
SKILLS_DIR = os.getenv("SKILLS_DIR", "/sandbox/skills")
MAX_TIMEOUT = int(os.getenv("MAX_TIMEOUT", "120"))
MAX_MEMORY_MB = int(os.getenv("MAX_MEMORY_MB", "256"))
MAX_NPROC = int(os.getenv("MAX_NPROC", "256"))
MAX_FSIZE_MB = int(os.getenv("MAX_FSIZE_MB", "10"))
