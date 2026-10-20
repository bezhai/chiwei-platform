"""她进入这一轮时读到的东西 —— 状态快照，不是历史回放。

**为什么不套 ``SessionTranscript``。** 那个有 200 条 / 256KiB 硬上限、每 100 条触发
一次模型折叠。她一天 144 轮，两天就撞顶，而且折叠频率跟着轮次的密度走：轮次越密、折叠
越频繁、失真越快。更根本的是折叠这个动作本身——它把"发生过什么"压成一段概括，压完
之后原文没了，压错了没人知道。

**换成状态快照。** 她读到的是**此刻的事实**，不是对历史的压缩：

  ==================  ==========================  ==============================
  层                  从哪读                      界从哪来
  ==================  ==========================  ==============================
  手上正在做的事      最新一条 ``Whereabouts``    1 行（"当前"只有一个）
  上一次写下的那天    ``read_day_page_before``    1 页（严格早于当前生活日的最新一页）
  她刚做过 / 说过     她自己的 ``Happening``      最近 N 条，只在她自己的话不在眼前时给
  ==================  ==========================  ==============================

还有一层「挂着没了结的事」由 loose_ends 插件自己读、自己摆（:mod:`app.living.loose_ends.mind`）；
「手上正在做的事」「她刚做过 / 说过」两层由 her_records 插件自己读、自己摆
（:mod:`app.living.her_records.book`）。这里只剩上一次写下的那天和"现在"。

**几层全是她自己的。** 别人做了什么、说了什么，不从这里读：谁会察觉由 world 判断后告诉她，
姐妹对她说的话直接送到她那里，都进她的收件箱，那一段由 :mod:`app.living.received` 读、由
:func:`app.living.moment.received_section` 摆进这一轮（它要跟这一轮在同一个事务里记成看过）。

**为什么这么长不会失真**：没有一层是"机器对历史的概括"。头两层是当下状态，读一
百遍字字一样；最后一层是原文照搬的最近若干条，只是**少**，不是**歪**。失真来自压缩，
这里一处压缩都没有。会被遗忘的只有最后一层滚出窗口的那些——而她心里挂着的那份清单正是她把
重要的东西从滚动窗口里救出来的那只手，救不救是她的决定（见 :mod:`app.living.loose_ends`）。

**这几层分两半送，因为她的上下文是连续的。** 每层一段，各是她一轮里的一个状态段或输入段
（:mod:`app.living.moment` 摆进去，清单 ``resident`` 定顺序）：

  * **她此刻的样子**：上一次写下的那天（:meth:`MomentSnapshot.render_day_page`）、刚做过说过
    什么（:meth:`app.living.her_records.book.OwnRecords.recent_section`）。读一百遍字字一样，所以每轮重发就是把同一段话
    抄一遍。只在清理那一下作为新起点重铺（:func:`app.agent.continuity.trim_for_round`），默认
    一小时一次。「刚做过说过什么」那段只在这份历史接不住她最近说过的话时才带（上下文是空的、
    硬顶把标记消息全裁掉了、上一轮没存下来，判据见 :func:`app.agent.continuity.continues_unbroken`）：
    其余时候她 4 小时内说过的话都原样在上下文里，每小时再抄一份就是同一句话在一轮输入里
    出现好几遍。
  * **这一轮的实况**：几点了、离上一次隔了多久（:meth:`MomentSnapshot.render_now`）、她在哪在做
    什么（:meth:`app.living.her_records.book.OwnRecords.whereabouts_section`）。每轮都送，因为每轮都不一样。

**日记那一层是她自己写的，不是折叠出来的。** 这是它跟被否掉的 ``SessionTranscript``
唯一但决定性的区别：一条原始记录都没被动过，那一页是她另写的一份东西（见
:mod:`app.living.day_page`）。少了它她跨不过一天——上面几层全是"当下"，滚出窗口的
东西没有任何一层接得住。这里只负责读和摆，"读哪一页"那条严格早于当前生活日的判据在
:func:`~app.living.day_page.read_day_page_before` 里。

**"她刚做过、说过"那层为什么必须单独存在**：她的上下文会被裁剪，传到她这里的消息也只摆
一次。上下文连着的时候她说过的话就在上下文里；上下文是空的、断过、上一轮没存下来的那一
轮，上下文接不住，少了这一层，她上一轮答应姐姐的话下一轮就凭空消失，"接得上昨天"永远
无从谈起。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.infra.cst_time import to_cst_full
from app.living.day_page import LivingDayPage, living_day_of, read_day_page_before


@dataclass(frozen=True)
class MomentSnapshot:
    """她这一轮读到的、她自己的那几层。"""

    lane: str
    persona_id: str
    now: datetime
    day_page: LivingDayPage | None

    # -- 这一轮的实况 ----------------------------------------------------

    def render_now(self, previous_at: datetime | None) -> str:
        """几点了、离上一次隔了多久。``previous_at`` 是上一个落地的 moment 的『现在』；一个都
        没跑过传 ``None``。"""
        # 完整口径（年月日 + 星期），不是裸时分：下面几段跨天的行渲染成 ``07-24 23:41
        # CST``，而这一轮喂给她的全部新东西就是这一段加手机信封（``app.living.moment``），
        # 没有第二个地方说今天几号 —— 不说的话 ``07-24`` 是昨天还是上个月她算不出来，
        # 记日程 / 算 ``remind_at`` 时更是只能瞎填日期分量。
        now = f"现在 {to_cst_full(self.now.isoformat())}"
        if previous_at is None:
            return f"{now}。"
        gap = self.now - previous_at
        # **不到一分钟（含负数）说"就在刚才"。** 常规 moment 的『现在』还记着格子的那些
        # 旧行，可能比先落地那个提前来的 moment 的真实时刻还早（见
        # :class:`app.living.moment.LifeMoment`），那时这个差是负的 —— 照直渲染就是往她
        # 眼前塞一句"离上一次过了 -4 分钟"。
        if gap < timedelta(minutes=1):
            return f"{now}，上一次就在刚才。"
        return f"{now}，离上一次过了 {_gap(gap)}。"

    # -- 她此刻的样子 ----------------------------------------------------
    #
    # 每段空的时候如实说空，不留白洞。**不带时刻**：界桩自己头上就印着这次清理的时刻，这里
    # 再报一次就是同一份输入里两个"现在"，而且两个数还不一样（界桩取整点，这一轮的 ``now``
    # 不是）。

    def render_day_page(self) -> str:
        """她上一次写下的那一天。它是背景，不是此刻正在发生的事。

        **日子必须印出来，而且"昨天"这个词只在真的是昨天时才用。** 服务停过几天、
        某天她一个字都没写下的时候，最近的一页可能是三天前的；把它说成"昨天"是往她
        眼前塞一句假话，而她会拿它当今天的前一天去接因果。
        """
        if self.day_page is None:
            return "你上一次写下的那一天：（还没有）"
        when = self.day_page.day.strftime("%m-%d")
        if self.day_page.day == living_day_of(self.now) - timedelta(days=1):
            head = f"昨天（{when}）你写下的："
        else:
            head = f"你上一次写下的是 {when}："
        return f"{head}\n{self.day_page.text}"


def _gap(delta: timedelta) -> str:
    """一段时长摆成她读得懂的样子：``10 分钟`` / ``4 小时`` / ``4 小时 30 分钟``。"""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时" if rest == 0 else f"{hours} 小时 {rest} 分钟"


async def read_snapshot(
    *, lane: str, persona_id: str, now: datetime
) -> MomentSnapshot:
    """读她这一轮自己的那几层。各层各读各的，谁也不裁谁。

    日记那一页按 ``day < 当前生活日`` 取最新的一页，**不是"最新一页"**：她凌晨写下
    的那页写的是刚过去那一天，而写完的那一刻已经属于新的生活日了。理由和它错了的样子
    写在 :func:`~app.living.day_page.read_day_page_before` 里。
    """
    return MomentSnapshot(
        lane=lane,
        persona_id=persona_id,
        now=now,
        day_page=await read_day_page_before(
            lane=lane, persona_id=persona_id, day=living_day_of(now)
        ),
    )
