"""Sandbox skill execution capability — Phase 7d Gap 16.

Calls the ``sandbox-worker`` service ``/exec`` endpoint via ``HTTPClient``.
``/exec`` runs a bash command, so it is non-idempotent: ``retries=0``.
Lane and trace headers are auto-injected by ``HTTPClient``.

**出量上限也在这一层**（:data:`OUTPUT_MAX_CHARS`）。沙箱那侧一个字都不截
（``executor.py`` 直接 ``stdout_bytes.decode(...)``；``MAX_FSIZE_MB`` 限的是写文件，
不是管道输出），agent-service 这侧的工具层也没有任何长度兜底 —— 工具返回多少就原样
进她这一轮的上下文。一条 ``python3 -c "print('x'*10**8)"``、或者某个脚本吐一大坨，
整轮就废了。

裁在这里而不是在调用方，是因为这个 capability 是**两条路唯一都要经过的地方**：她自己
跑一条命令（``app.living.guides.run_a_script``），和一份说明里那条 ``!`cmd``` 的结果被
替换进正文（``app.skills.renderer.render_skill``，那一份同样没有上限）。裁在调用方就
是两份实现、迟早漏一条；将来多一个调用方还会再漏一次。

**截了必须说出来。** 静默截断比截断本身更糟：她读到一段戛然而止的输出会当成那就是
全部，然后拿一个不完整的结果往下做事（同类判断见 ``app.living.day_page`` 里"留白她会
以为材料被截断了，转去补一段自己编的前情"）。所以裁完在后面接一句实话，说清楚还剩多
少没给她、以及怎么让它少打点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.capabilities.http import HTTPClient
from app.infra.config import settings

logger = logging.getLogger(__name__)

# service="sandbox-worker" → LaneRouter resolves the right lane.
# retries=0 + retry_post=0: /exec is non-idempotent (executes a command).
# Timeout = command_timeout + 15s buffer; default 30s + 15s = 45s.
_CLIENT = HTTPClient(service="sandbox-worker", timeout=45.0, retries=0)

# 一次交回她多少字（stdout / stderr **各**按这个数裁）。
#
# 取的是"她这一轮眼前最大的那件东西"同一个量级：上网查那只手一屏是 5 条命中 × 每条
# 800 字摘录 ≈ 4000 字（``app.agent.tools.search`` 里那个 ``[:800]``），读书一程是
# 1800 字（``app.domain.reading_source.DEFAULT_PAGE_SIZE``），快照那几层各是几十条短
# 行。再往下会切掉正常脚本的正常输出（搜一次番剧条目就是几 KB），再往上就开始跟她本
# 来该看的东西抢地方 —— 那正是这条上限要挡的事。
#
# 两股各按这个数裁而不是共用一个总额：跑挂那一路 stdout（挂之前跑到哪儿了）和 stderr
# （具体哪儿错了）都要给她，合并一个总额会让先到的那股把另一股挤没。
OUTPUT_MAX_CHARS = 4_000

# 截过之后接在后面那句话里的固定一段。**只在这里定义一次**：她读到的是它，调用方
# 事后想认出"这段被截过"认的也是它（``app.living.guides.read_a_guide`` 那条路拿不到
# 计数，只看得见正文）。两边各写一遍字面量的话，改一个字就认不回来了。
OUTPUT_CUT_MARK = "个字没给你"


def _cut(text: str) -> tuple[str, int]:
    """裁到上限，交回 ``(给她的那段, 砍掉了多少字)``。纯函数。

    没超上限的原样返回、一个字不加：裁的是刷屏那种，不是给每条输出都缀一句废话。
    """
    if len(text) <= OUTPUT_MAX_CHARS:
        return text, 0
    dropped = len(text) - OUTPUT_MAX_CHARS
    notice = (
        f"\n\n……（这一段太长，后面还有 {dropped} {OUTPUT_CUT_MARK} —— "
        f"一次最多给 {OUTPUT_MAX_CHARS} 字。想看全就让它少打点：后面接 | head、"
        f"或者用它自己的参数把范围缩小，再跑一次。）"
    )
    return text[:OUTPUT_MAX_CHARS] + notice, dropped


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    # 交回之前砍掉了多少字（两股合计）。0 = 一个字没砍。调用方靠它留痕：事后要查得出
    # 哪一轮被截过（langfuse 会系统性丢 trace，日志是唯一查得到的地方）。
    dropped: int = 0


async def run(
    *,
    command: str,
    skill_name: str = "",
    envs: dict[str, str] | None = None,
    timeout: int = 30,
) -> SandboxResult:
    """Execute ``command`` in the sandbox; returns structured result.

    两股输出各按 :data:`OUTPUT_MAX_CHARS` 裁，超了在后面接一句实话并计进
    ``dropped``（见模块 docstring：裁在这里，两条路谁也漏不掉）。

    Args:
        command: bash command to run.
        skill_name: skill working-dir hint for the sandbox.
        envs: extra env vars to inject.
        timeout: command timeout (seconds).

    Raises:
        httpx.HTTPStatusError: when the sandbox-worker returns non-2xx.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.inner_http_secret:
        headers["Authorization"] = f"Bearer {settings.inner_http_secret}"
    resp = await _CLIENT.post(
        "/exec",
        json={
            "command": command,
            "skill_name": skill_name,
            "envs": envs or {},
            "timeout_sec": timeout,
        },
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    stdout, out_cut = _cut(data["stdout"])
    stderr, err_cut = _cut(data["stderr"])
    if out_cut or err_cut:
        # 这一层只知道跑的是哪条命令、哪份 skill；带 moment 身份那条痕由调用方留
        # （``app.living.guides``），两条一起才查得出"哪一轮读到的是不全的东西"。
        logger.warning(
            "sandbox output capped: dropped %d chars (skill=%s, command=%s)",
            out_cut + err_cut,
            skill_name or "(none)",
            command,
        )
    return SandboxResult(
        exit_code=data["exit_code"],
        stdout=stdout,
        stderr=stderr,
        dropped=out_cut + err_cut,
    )
