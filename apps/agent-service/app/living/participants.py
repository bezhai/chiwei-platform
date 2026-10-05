"""life 这一侧认识的参与者：三姐妹自己，和 world。名字就是她们在通信机制里的地址。

**三姐妹用的是她们在世界里的名字，取自人设表的显示名**（``bot_persona.display_name``），
代码里不写任何一个名字。``akao`` 这类 id 只留在 life 内部：world 和它发来的告知里用的都
是世界里的名字，她看到的、说出去的应该是同一套名字。

**名字就是地址，所以进程启动时先检查**（:func:`load_residents`），下面任何一种情况都拒绝
启动，不带病运行：

  * 人设表里没有这个人，或者她的显示名是空的；
  * 显示名不符合通信机制的名字规则（:func:`app.messaging.message.participant`）；
  * 两个人的显示名一样——发给其中一个的消息会进另一个的收件箱；
  * 显示名跟已有的参与者撞了（:data:`WORLD`、``operator``）——发给她的消息会进别人的收件箱。

几处同时有问题时一次报全：名字改在人设表里，改一处、重启、再撞下一处太慢。

**开收件箱用的对照和以后发消息时查地址用的对照是同一份**：启动时读一次，之后都从
:func:`residents` 拿。不支持改名：改了显示名，对 world 来说就是一个新的人，旧名字的队列和
world 按旧名存下的东西都不会迁移。

**world 的名字在这里另写一份**，不从 :mod:`app.world` import：life 和 world 互不 import
（CI 规则 ``scripts/check_world_life_imports.py``）。两份由
``tests/living/test_participants.py`` 钉在一起。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.data.queries import find_persona
from app.living.persona import LIVING_PERSONAS
from app.messaging.message import participant
from app.messaging.operator import OPERATOR

# world 在通信机制里的名字。它发来的是她察觉到的事，不是谁对她说的话（见
# :func:`app.living.received.render_received`）。
WORLD = "world"

# 已经有主的名字。姐妹的显示名撞上其中一个，发给她的消息就会进别人的收件箱。
_TAKEN = (WORLD, OPERATOR)


@dataclass(frozen=True)
class Residents:
    """三姐妹的 persona_id 和她们在世界里的名字，一一对应。"""

    by_persona: dict[str, str]

    def persona_of(self, name: str) -> str | None:
        """叫这个名字的是谁；不是三姐妹之一返回 ``None``。"""
        for persona_id, known in self.by_persona.items():
            if known == name:
                return persona_id
        return None

    def names_for(self, who: list[str]) -> list[str]:
        """"说给谁"里三姐妹的 id 换成她在世界里的名字，其余原样。id 和名字的对照只在这里。

        两处用它：

          * ``say`` 记下这句话时（:func:`app.living.moment._record`）。她在 ``to`` 里写了姐妹的
            id，记下来的就是姐妹的名字，发消息时按名字认出是姐妹、直接送到她那里，跟写名字
            一样。
          * 她记下的"说给谁"摆到她眼前时（:func:`app.living.happening.own_line`）。``say`` 以前
            原样记 id，那些旧记录里是 ``chinagi`` 这种；她看到它，下一次就照着它去叫人。

        不是三姐妹的（世界里的人、写错的名字、写错的 id）原样留着，那是她写下的，她想叫的是谁
        life 不猜。同一个人写了两种叫法只留一次，按出现的先后。
        """
        return list(dict.fromkeys(self.by_persona.get(w, w) for w in who))

    def sisters_in(self, names: list[str], *, speaker: str) -> list[str]:
        """``names`` 里哪几个是 ``speaker``（persona_id）的姐妹：三姐妹之一、又不是她自己。

        按出现的先后，同一个名字只留一次。不是姐妹名字的（世界里别的人、写错的名字）都不在
        里面——那是"世界里的某个人"，由 world 判断有没有这个人、听没听见。她写的姐妹 id 在
        记下时已经换成了名字（:meth:`names_for`），到这里的就是名字。
        """
        found: list[str] = []
        for name in names:
            persona_id = self.persona_of(name)
            if persona_id is not None and persona_id != speaker and name not in found:
                found.append(name)
        return found


# 启动时读好的那一份（:func:`load_residents`）。读之前是 ``None``。
_known: Residents | None = None


async def load_residents() -> Residents:
    """从人设表读三姐妹的名字、逐条检查；全部合格才记下这份对照并交回，否则抛 ``RuntimeError``。"""
    global _known
    problems: list[str] = []
    by_persona: dict[str, str] = {}
    for persona_id in LIVING_PERSONAS:
        persona = await find_persona(persona_id)
        if persona is None:
            problems.append(f"{persona_id}：人设表里没有这一行")
            continue
        name = persona.display_name
        if not name or not name.strip():
            problems.append(f"{persona_id}：显示名是空的")
            continue
        try:
            participant(name)
        except ValueError:
            problems.append(
                f"{persona_id}：显示名 {name!r} 不符合通信机制的名字规则"
                f"（只收字母、数字、-、_，以字母或数字开头，见 app.messaging.message.participant）"
            )
            continue
        if name in _TAKEN:
            problems.append(f"{persona_id}：显示名 {name!r} 跟已有的参与者重名")
            continue
        by_persona[persona_id] = name

    owners: dict[str, list[str]] = {}
    for persona_id, name in by_persona.items():
        owners.setdefault(name, []).append(persona_id)
    for name, persona_ids in owners.items():
        if len(persona_ids) > 1:
            problems.append(f"{'、'.join(persona_ids)}：显示名都是 {name!r}")

    if problems:
        raise RuntimeError(
            "三姐妹的显示名就是她们在通信机制里的地址，下面这些改好之前不开收件箱：\n  "
            + "\n  ".join(problems)
        )
    _known = Residents(by_persona)
    return _known


def residents() -> Residents:
    """启动时读好的那份对照。还没读就抛：那是在通信机制开始接收之前就来查地址了。"""
    if _known is None:
        raise RuntimeError(
            "三姐妹的名字还没读：通信机制开始接收时才读（app.living.received.open_inboxes）"
        )
    return _known
