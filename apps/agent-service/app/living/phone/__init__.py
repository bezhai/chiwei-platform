"""phone 插件：她的手机 —— 通知、会话列表、会话详情三层，有动静她看得见，内容要她自己拿起来看。

  * 三只手（:class:`app.living.phone.handset.Handset` 的方法）：``look_at_phone`` 打开一条会话、
    ``look_through_your_phone`` 翻一翻会话列表、``look_up_contact`` 按名字找回一条会话。
  * 状态段 ``phone.unread``：手机上还没看的全貌；输入段 ``phone.arrived``：手机上刚来的。两段是同一次
    查到的信封。
  * 落地那个事务里的 ``phone.glances``：她这一轮看过的会话记成已读，跟这一轮一起落地。
  * 交出 ``phone`` 服务（:class:`app.living.services.Phone`）：发消息、撤回、读文件、提前叫醒她的那条钟
    经它问她手机上有什么。哪些会话在她手机上只有一道闸，视野在她的一轮之内只定一次
    （:mod:`app.living.phone.whitelist`）。
  * 要 ``her_records`` 服务（信封上"你上次在这儿开口是什么时候"）和 ``pictures`` 服务（她发过的图
    还在不在她手上、印成什么句柄）。

三层各给什么、已读怎么推、谁算主人，见 :mod:`app.living.phone.handset`。
"""
from __future__ import annotations

from app.agent.tooling import Tool
from app.host import Context, Plugin
from app.living.phone.handset import Handset


def setup(ctx: Context) -> None:
    phone = Handset(her_records=ctx.service("her_records"), pictures=ctx.service("pictures"))
    ctx.provide("phone", phone)
    # 手机的三只手都留着，不当素材裁：
    #
    #   * ``look_up_contact`` / ``look_through_your_phone``  ``channel_id=<id>``，被 ``look_at_phone`` /
    #     ``send_message`` 吃（翻页那只手的最后一串还是往下翻的游标）。安静下来的会话不在手机通知上，
    #     这两只手是找回它的仅有的两条路。
    #   * ``look_at_phone``  **唯一一只返回是别人内容、却留着的手。** 它那一页上有两样凭据：每条她自己
    #     发的消息带的 ``take_back_id``，和头上那串 ``before=``。前者在快照的"你刚做过、说过"那段有
    #     副本，但那段只有最近 12 条，滚出去的旧消息就没有第二份了；后者是往前翻**唯一**的入口，从头
    #     到尾只出现在这一次返回里，换掉正文她就再也翻不回这条会话更早的地方。只保留句柄、把正文
    #     换掉更精确，但那要在裁剪这一层解析手机这边渲染出来的标签 —— 渲染改一个字，句柄就静默地留
    #     不住了，而症状是几小时后她拿着一个不存在的编号去撤消息。别人的正文因此多留 3 小时：一页十几
    #     条文本，跟这个风险不是一个量级（图片块**不**跟着多留，它走
    #     ``HistoryTexts.picture_trimmed``（:mod:`app.manifests.history`） 那条独立的线）。
    ctx.tool(Tool(phone.look_at_phone), trim="kept")
    ctx.tool(Tool(phone.look_through_your_phone), trim="kept")
    ctx.tool(Tool(phone.look_up_contact), trim="kept")
    ctx.section("phone.unread", phone.unread_section)
    ctx.section("phone.arrived", phone.arrived_section)
    ctx.commit("phone.glances", phone.commit_glances)


PLUGIN = Plugin(
    name="phone",
    setup=setup,
    requires=("her_records", "pictures"),
    provides=("phone",),
)
