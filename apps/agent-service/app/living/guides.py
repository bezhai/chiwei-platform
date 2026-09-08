"""她手边那几份写好的说明 —— 读得到、知道有哪些，也跑得动说明里教她跑的东西。

**说明是别人替她写好、放在盘上的正文**（NFS 上一份一个目录，PVC 只读挂到
``SKILLS_DIR``）：她自己长什么样写在 ``drawing`` 那一份里，另外几份教她查番剧条目、
查同人展。底下那套机制（扫目录、解析、热加载、渲染）住在 :mod:`app.skills`，本模块
只是她这一轮伸出去的两只手 —— 跟 :mod:`app.living.pictures` 之于
:mod:`app.agent.image_gen` 是同一个分法。

两只手
------

  * :func:`read_a_guide`  把其中一份从头读一遍；
  * :func:`run_a_script`  照着那份说明，把它教她跑的那条命令在沙箱里真的跑一遍。

**第二只手不是附赠的。** 四份说明里三份通篇在教她跑 ``$SKILL_DIR/scripts/`` 下的脚本
—— 只接读取的话，读完那三份她手上没有任何能跑它的东西，等于读了个寂寞。

**跑的时候必须带上是哪份说明教的**（``guide`` → capability 的 ``skill_name``）。沙箱那
边拿到它才会把那份说明的 ``scripts/`` 软链进工作目录、并注入 ``SKILL_DIR`` 环境变量；
不带的话说明里那条命令当场找不到文件。旧的 ``sandbox_bash`` **从来不传这个参数**，那
三份说明教她的用法在它上面一步都走不通。名字写错在这里就拦住：打下去的话沙箱只是安静
地不软链，报错会指向"文件不存在"，而真正错的是这个名字。

**她知道有哪些可读，只能从 prompt 变量进**（:data:`GUIDES_VAR`）。工具 schema 在
import 时就定死了，而注册表是启动时填、每 30 秒热加载的 —— 把清单写进 docstring 或者
焊成参数的 ``enum``，盘上一改她读到的就是过期的那份。清单本身走
:meth:`app.skills.registry.SkillRegistry.list_descriptions`。

**一份都没有的时候要说一句实话，不能交回空串。** 注册表为空是真会发生的：目录没挂上
时 ``load_all`` 记一条 warning 就留一个空注册表。交回空串的话她那段说明渲染出来是一个
空标题，她读到的是"这里本该有东西"，而不知道到底有没有。

**这两只手一个字都不落库。** 读回来的正文、跑出来的结果只进这一轮的上下文，下一轮就没了
（跟 :mod:`app.living.web` 同一条）。她想把什么留到下一轮，用她自己那份"心里挂着没了
结的事"（:func:`app.living.moment.keep_in_mind`）。

沙箱的边界按现状写，不夸大
--------------------------

``apps/sandbox-worker`` 是 tempdir + resource limits + 命令黑名单正则：按命令名封
``curl`` / ``wget`` / ``nc`` / ``ssh`` 这些（``ALLOWED_NETWORK_COMMANDS`` 可放行），
外加提权、装包、``rm /``、读 shadow、改权限、``dd`` 写盘。**没有网络命名空间隔离，
Python 脚本内部发 HTTP 不受这条限制** —— 那三份说明的脚本正是靠这一点才成立。旧
``sandbox_bash`` 的 docstring 里那句「限制：无网络访问」是错的，重写不照抄。

脚本要的凭据走 ``SANDBOX_`` 前缀的环境变量在沙箱那侧透传，模型看不到值，所以工具签名
里没有 ``envs``：她不需要、也不该经手任何一个凭据。

**跑出来的东西有上限，裁在 :data:`app.capabilities.sandbox.OUTPUT_MAX_CHARS`**，不在
这里。沙箱那侧一个字都不截，而这两只手走的是同一个 capability（说明里那条预处理指令
的结果也是），裁在调用方就是两份实现、迟早漏一条。这一层只做两件事：把那句"还剩多少
没给你"原样带到她眼前，以及留一条带 moment 身份的痕 —— 事后要查得出哪一轮读到的是不全的。

转义的边界划在哪儿
------------------

她这一轮的输入是结构化的（``<msg from=".." rel="owner">``，见 :mod:`app.living.phone`），
所以外面来的字串进去之前要过 :func:`app.living.records.esc`。这两只手上那条界是这么
划的：

  * :func:`run_a_script` 打出来的 ``stdout`` / ``stderr`` **过**。沙箱没有网络命名空间
    隔离，脚本自己上网是这几份说明成立的前提 —— "抓一个网页、把内容 print 出来"于是跟
    :func:`app.living.web.browse_online` 是同一条逐字通道，那几个字节由网页那边决定。
  * :func:`read_a_guide` 交回的**说明正文不过**。那是主人自己写在 NFS 上的 markdown，
    按"外面来的"这条口径他不是外面：能改那个目录的人已经能改她读到的一切。整段转义
    只会把他写的 markdown 糟蹋掉。
  * 正文里嵌的那段预处理输出（说明里的 ``!`cmd``` 由 :mod:`app.skills.renderer` 跑）
    **也不过**，而这一条是**明知的例外**：理论上它跟上面第一条同级。不堵是因为那些
    脚本是我们自己的、打的是 ``--help`` 这类固定内容，而要堵就得堵进
    :mod:`app.capabilities.sandbox`——那一层同时供着 :func:`render_skill`，在那儿转义
    会连主人的正文一起转掉。风险和代价不成比例，所以界划在这儿。**哪天说明里那条预
    处理指令开始打外面来的东西，这条例外就不成立了。**
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error

# 模块级引用，测试从这里换替身（真跑起来是一次打到 sandbox-worker 的 HTTP）。
# 同 :mod:`app.skills.renderer` 的写法。
from app.capabilities.sandbox import OUTPUT_CUT_MARK
from app.capabilities.sandbox import run as _sandbox_run
from app.living.records import esc
from app.living.scope import moment_scope
from app.skills.registry import SkillRegistry
from app.skills.renderer import render_skill

logger = logging.getLogger(__name__)

run = _sandbox_run

# 她这一轮的 prompt 变量名：手边这些说明各叫什么、是讲什么的。**只在这里定义一次**
# —— 变量名没有编译期校验，两处各写一遍字面量，改一个字就让 Langfuse 那侧原样渲染出
# ``{{...}}`` 摆到她眼前。
GUIDES_VAR = "guides_you_can_read"

# 一份都没有时摆给她的那句话。**不能是空串**：她那段说明会渲染成一个空标题。
_NOTHING_ON_HAND = (
    "（这会儿你手边一份说明都没有 —— 不是你记错了，是它们没送到这儿来。）"
)


def guides_she_can_read() -> str:
    """这一轮她手边有哪些说明可读，摆成 prompt 里那一段。

    每轮现算：注册表每 30 秒热加载一次，缓存下来就等于给她一份会过期的清单。
    """
    return SkillRegistry.list_descriptions() or _NOTHING_ON_HAND


def _which_ones_she_can_read() -> str:
    """名字对不上时交给她的那句话：能读的都有哪些。

    一份都没有和名字写错是两种处境，给她的话也必须是两句 —— 前者再试一百次也没有，
    后者换个名字就有了。
    """
    listing = SkillRegistry.list_descriptions()
    if not listing:
        return _NOTHING_ON_HAND
    return f"你手边能读的是这几份：\n{listing}"


@tool
@tool_error("这份说明没读成")
async def read_a_guide(
    which: Annotated[
        str,
        Field(description="你要读哪一份，写它的名字（清单上冒号前面那个词）"),
    ],
) -> str:
    """把手边的一份说明从头读一遍。

    有几份写好的说明一直在你手边，你眼前那份清单列着它们各自叫什么、讲的是什么。
    想不起自己长什么样、要查一部番的条目、要找哪天哪儿有同人展 —— 先把对应那份读
    了，该怎么做写在里面。

    **读不读是你自己的事。** 清单上那几行只有名字和一句话，正文得读了才知道；没有
    谁会在你该读的时候提醒你。

    读回来的内容只在这一轮里跟着你，下一轮就没了，那时想用就再读一遍。

    有的说明里带着一条"先跑一下看看"的命令，你读到的是它跑出来的结果；这种结果
    **太长会被截掉**，截了多少就写在那儿 —— 看到那句话就知道这一份你读到的不是全的。

    说明里教你跑某个脚本的时候，把它写的那条命令交给 run_a_script 跑，别自己编路径，
    也别用 act 假装跑过 —— 你心里那些"跑出来的结果"全是自己编的。

    名字写错了它会把你能读的几份摆给你，照着挑一个再来。

    Args:
        which: 你要读哪一份说明。

    Returns:
        那份说明的正文；没有这一份时，一句实话加上你能读的都有哪些。
    """
    lane, _now, persona_id, moment_id = moment_scope()
    name = which.strip()
    if not name:
        raise ValueError(f"你没说要读哪一份。{_which_ones_she_can_read()}")

    try:
        skill = SkillRegistry.get(name)
    except KeyError:
        # 注册表自己那条 KeyError 的消息带引号、是给排查的人看的。交给她的话得是
        # 她能照着做的：这个名字没有，你能读的是这几份。
        raise ValueError(
            f"你手边没有叫「{name}」的说明。{_which_ones_she_can_read()}"
        ) from None

    body = await render_skill(skill)
    logger.info(
        "living guides lane=%s persona=%s moment=%s 读：%s（%d 字）",
        lane,
        persona_id,
        moment_id,
        name,
        len(body),
    )
    # 说明里那条 ``!`cmd``` 的结果太长时，capability 那一层已经裁过并在正文里留了话。
    # 这条路拿不到计数（``render_skill`` 只交回正文），所以认那句话里那一段固定的字
    # —— 它跟印出去的是同一处定义。误判的代价只是多一行日志。
    if OUTPUT_CUT_MARK in body:
        logger.info(
            "living guides lane=%s persona=%s moment=%s 说明 %s 里有一段跑出来的"
            "东西被截过，她读到的这份不全",
            lane,
            persona_id,
            moment_id,
            name,
        )
    return body


@tool
@tool_error("这条命令没跑成")
async def run_a_script(
    command: Annotated[
        str,
        Field(
            description="要跑的那条命令，说明里怎么写的就怎么抄，例如 "
            "python3 /sandbox/skills/bangumi/scripts/bangumi.py search 孤独摇滚"
        ),
    ],
    guide: Annotated[
        str,
        Field(
            description="这条命令是哪份说明教你的，写那份的名字；"
            "不是从说明里来的（自己算点什么）就别填"
        ),
    ] = "",
) -> str:
    """在一台干净的机器上跑一条命令。

    read_a_guide 读到的说明教你跑脚本时用它：把说明里那条命令原样抄进来，guide 写
    那份说明的名字。**从说明里抄来的命令，guide 必须写** —— 带上它，那份说明的脚本
    才摆在你跑命令的地方，脚本要用的账号密码也已经在那台机器上（你不用知道、也拿不到
    那些值）；不写的话你抄的那条命令会告诉你文件不存在。

    自己想算点什么也可以用它 —— 一串数怎么加、一段文本有多少字、某天是星期几 ——
    写一小段 python3 就行，这种时候 guide 空着。

    跑不通它会把退出码和报错原样给你。你自己看是哪儿写错了、要不要换个写法再来一次。

    打出来的东西**太长会被截掉**，只给你前面一段，后面还剩多少它会告诉你。看到那句话
    就别把手上这段当成全部 —— 想看全就让它少打点：后面接 | head、用它自己的参数把范围
    缩小，或者分几次跑。

    几件事它做不了：一条命令最多跑 30 秒，超了会被掐掉；curl、wget、ssh 这类命令直接
    跑不了（说明里的脚本自己上网不受这条管）；装包、改权限、动系统目录也一样不行。

    这台机器跟你的手机、跟这个家都没有关系：在上面跑的东西谁也看不见，也改不了这儿的
    任何事。想让别人知道什么，还得你自己说出口。

    Args:
        command: 要跑的那条命令。
        guide: 这条命令是哪份说明教你的。

    Returns:
        命令打出来的东西；没跑通时退出码、打出来的和报错一起原样交给你。
    """
    lane, _now, persona_id, moment_id = moment_scope()
    line = command.strip()
    if not line:
        raise ValueError("你没说要跑什么：写一条命令。")

    # 名字写错在这里就拦住 —— 打下去的话沙箱只是安静地不软链那份脚本，她收到的报错
    # 会指向"文件不存在"，而真正错的是这个名字。
    from_guide = guide.strip()
    if from_guide:
        try:
            SkillRegistry.get(from_guide)
        except KeyError:
            raise ValueError(
                f"你手边没有叫「{from_guide}」的说明，所以它的脚本也不在你手边。"
                f"{_which_ones_she_can_read()}"
            ) from None

    logger.info(
        "living guides lane=%s persona=%s moment=%s 跑（%s）：%s",
        lane,
        persona_id,
        moment_id,
        from_guide or "不属于任何一份说明",
        line,
    )
    result = await run(command=line, skill_name=from_guide)
    if result.dropped:
        # 带 moment 身份的那条痕：事后要查得出哪一轮读到的是不全的东西。capability 那层也记
        # 了一条（命令 + 砍了多少），但它不知道这是谁的哪一轮。
        logger.info(
            "living guides lane=%s persona=%s moment=%s 跑出来的东西太长，"
            "砍掉 %d 字才交给她：%s",
            lane,
            persona_id,
            moment_id,
            result.dropped,
            line,
        )

    # 打出来的两股都过 :func:`esc`。**沙箱没有网络命名空间隔离**（见模块 docstring），
    # 那几份说明的脚本正是靠这一点上网 —— 所以"脚本抓一个网页、把内容 print 出来"跟
    # :func:`app.living.web.browse_online` 是同一条逐字通道：那几个字节由网页那边决定，
    # 原样进她眼前，而她这一轮的输入里还摆着 ``<msg from=".." rel="owner">`` 那几行。
    #
    # **转义落在这只手上，不落进 capability。** 那个 capability 还被
    # :func:`app.skills.renderer.render_skill` 用着，而说明正文是主人自己写在 NFS 上的
    # markdown，在那一层整段转义会把他的正文糟蹋掉。
    if result.exit_code != 0:
        # 三样一起给她：退出码说明是怎么挂的，stdout 是挂之前跑到哪儿了，stderr 是
        # 具体哪儿错了。只给一样她就得靠猜。
        return (
            f"这条命令退出码 {result.exit_code}。\n"
            f"打出来的：\n{esc(result.stdout)}\n"
            f"报错：\n{esc(result.stderr)}"
        )
    if not result.stdout.strip():
        # 空串交回去，她看到的是一个什么都没有的工具结果，分不清是没跑还是没输出。
        return "跑完了，它一个字都没打出来（退出码 0，就是没有输出）。"
    return esc(result.stdout)


GUIDE_TOOLS = [read_a_guide, run_a_script]
