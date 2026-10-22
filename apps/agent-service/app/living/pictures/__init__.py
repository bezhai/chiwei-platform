"""pictures 插件：她自己做过的图 —— 画一张、上网找一张、翻一翻手上有什么、拿出其中一张看。

  * 四只手（:class:`app.living.pictures.album.Album` 的方法），返回里都留着 ``pic=<32 位十六进制>``：
    它们是她后面还要原样抄回去的凭据（``send_message(pictures=[...])`` /
    ``look_at_a_picture(which=...)`` / ``look_through_your_pictures(before=...)`` 吃；翻页那只手的
    最后一串还是往前翻的游标），所以跟她自己的话一样留着，不当素材裁。
  * 交出 ``pictures`` 服务（:class:`app.living.services.Pictures`）：手机和嘴经它认她的图。

存什么、怎么认、为什么"看"要拆成两只手，见 :mod:`app.living.pictures.album`。
"""
from __future__ import annotations

from app.agent.tooling import Tool
from app.host import Context, Plugin
from app.living.pictures.album import Album


def setup(ctx: Context) -> None:
    album = Album()
    ctx.provide("pictures", album)
    ctx.tool(Tool(album.draw_a_picture), trim="kept")
    ctx.tool(Tool(album.find_a_picture_online), trim="kept")
    ctx.tool(Tool(album.look_through_your_pictures), trim="kept")
    ctx.tool(Tool(album.look_at_a_picture), trim="kept")


PLUGIN = Plugin(name="pictures", setup=setup, provides=("pictures",))
