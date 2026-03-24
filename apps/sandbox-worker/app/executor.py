"""沙箱命令执行器

在隔离子进程中执行命令，通过 tmpdir + resource limits + timeout 实现安全隔离。
"""

import asyncio
import logging
import os
import re
import resource
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import ALLOWED_NETWORK_COMMANDS, MAX_FSIZE_MB, MAX_MEMORY_MB, MAX_NPROC, SKILLS_DIR

logger = logging.getLogger(__name__)

# 子进程中允许的最小环境变量集
_SAFE_ENV_KEYS = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH"}

# 所有网络命令（可通过 ALLOWED_NETWORK_COMMANDS 配置放行）
_ALL_NETWORK_COMMANDS = {"curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "ftp", "telnet"}

# 始终封锁的命令（不可配置）
_ALWAYS_BLOCKED_PATTERNS = [
    r"\b(sudo|su|chroot|nsenter|mount|umount)\b",  # 权限提升
    r"\b(apt|yum|pip|pip3)\s+install\b",            # 包安装
    r"\brm\s+(-[rfR]+\s+)?/",                       # rm 根目录
    r"\bcat\s+/etc/(shadow|passwd|hosts)",           # 敏感文件读取
    r"\b(chmod|chown)\b",                            # 权限修改
    r"\bdd\b.*\bof=/",                               # dd 写磁盘
]


def _build_blocked_patterns() -> list[re.Pattern]:
    """根据配置构建命令黑名单正则。"""
    patterns = [re.compile(p) for p in _ALWAYS_BLOCKED_PATTERNS]

    # 计算需要封锁的网络命令
    if ALLOWED_NETWORK_COMMANDS.strip() == "*":
        blocked_net = set()  # 全部放行
    elif ALLOWED_NETWORK_COMMANDS.strip():
        allowed = {c.strip() for c in ALLOWED_NETWORK_COMMANDS.split(",") if c.strip()}
        blocked_net = _ALL_NETWORK_COMMANDS - allowed
    else:
        blocked_net = _ALL_NETWORK_COMMANDS  # 默认全部封锁

    if blocked_net:
        net_pattern = r"\b(" + "|".join(sorted(blocked_net)) + r")\b"
        patterns.append(re.compile(net_pattern))

    return patterns


_BLOCKED_RE = _build_blocked_patterns()


def _validate_command(command: str) -> None:
    """校验命令是否包含受限操作。"""
    for pattern in _BLOCKED_RE:
        if pattern.search(command):
            raise ValueError(f"命令包含受限操作，已拒绝执行")


@dataclass
class ExecutionResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: float = 0


def _set_resource_limits() -> None:
    """在子进程中设置资源限制（preexec_fn 回调）。

    注意：RLIMIT_AS 限制虚拟内存地址空间，Python 启动就需要 ~400MB 虚拟内存，
    所以设为物理内存限制的 4 倍。实际内存由 K8s 容器 limits 约束。
    """
    # 虚拟内存上限（宽松，实际内存由 K8s limits 控制）
    virt_bytes = MAX_MEMORY_MB * 4 * 1024 * 1024
    fsize_bytes = MAX_FSIZE_MB * 1024 * 1024

    resource.setrlimit(resource.RLIMIT_AS, (virt_bytes, virt_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    # RLIMIT_NPROC 在容器中有效（容器中用户独享），开发机上跳过
    # 由 K8s Pod resource limits 控制进程资源


def _build_env(extra_envs: dict[str, str] | None = None) -> dict[str, str]:
    """构建子进程环境变量（最小集 + SANDBOX_ 透传 + 用户自定义）。"""
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    # SANDBOX_ 前缀的环境变量透传给子进程（如 SANDBOX_DB_URL）
    # 这些变量在 K8s 部署时配置，脚本可直接读取，LLM 看不到
    env.update({k: v for k, v in os.environ.items() if k.startswith("SANDBOX_")})
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", "/tmp")
    env.setdefault("TZ", "Asia/Shanghai")
    if extra_envs:
        env.update(extra_envs)
    return env


async def execute(
    command: str,
    skill_name: str = "",
    timeout: int = 30,
    envs: dict[str, str] | None = None,
) -> ExecutionResult:
    """在隔离子进程中执行命令。

    隔离机制:
    - 独立临时目录（执行后清理）
    - 资源限制（内存/进程数/文件大小）
    - 超时控制
    - 最小环境变量

    Args:
        command: bash 命令
        skill_name: 技能名称（设置工作目录到对应 skill 的 scripts 目录）
        timeout: 超时秒数
        envs: 额外环境变量
    """
    # 命令安全校验
    _validate_command(command)

    start = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmpdir:
        # 如果有 skill_name，将 skill scripts 软链接到工作目录
        cwd = tmpdir
        if skill_name:
            skill_dir = Path(SKILLS_DIR) / skill_name
            if skill_dir.exists():
                scripts_src = skill_dir / "scripts"
                if scripts_src.exists():
                    scripts_dst = Path(tmpdir) / "scripts"
                    scripts_dst.symlink_to(scripts_src)

        env = _build_env(envs)
        # 注入 SKILL_DIR 让脚本能找到自己的资源
        if skill_name:
            env["SKILL_DIR"] = str(Path(SKILLS_DIR) / skill_name)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                preexec_fn=_set_resource_limits,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            return ExecutionResult(
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
                duration_ms=_elapsed_ms(start),
            )

        except asyncio.TimeoutError:
            # 超时，杀死子进程
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return ExecutionResult(
                stderr=f"execution timed out after {timeout}s",
                exit_code=124,
                duration_ms=_elapsed_ms(start),
            )

        except Exception as e:
            logger.error("sandbox execution error: %s", e, exc_info=True)
            return ExecutionResult(
                stderr=str(e),
                exit_code=1,
                duration_ms=_elapsed_ms(start),
            )


def _elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 2)
