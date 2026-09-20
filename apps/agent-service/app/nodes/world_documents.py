"""从外面读写世界文档树的四个 handler。

这四个 handler 自己**一行都不碰盘**：读、写、删全部交给
:mod:`app.living.documents` 那四个入口，走的是 world 五只手真正碰盘时抢的同一把
per-file 锁、同一套指纹。自己开一条写入路径的话，那条路径不在锁里也不认指纹，而它改的
是同一棵树 —— 外面这一侧的每一条验收都拦不住它，因为它们验的是这四个端点走的那一条。

这一层做的只有两件事：

* **把结果翻成 HTTP。** :class:`app.living.documents.DocumentChange` 的 ``outcome``
  本来就是给程序读的，直接映射成状态码 —— 那正是它存在的理由。不调
  ``raise_if_refused()``：那只手是把结果变回模型那一侧的异常和中文，翻一道再翻回来
  就是绕了一圈回到"解析中文"。
* **每个回答都带上执行它的那个进程自己的泳道。** 见 :func:`_lane`。

状态码：

* 写成 / 读到 / 列到 → 200
* 三种指纹冲突（没带、过期、那一份已经没了）→ 409，``outcome`` 说是哪一种。
  三种共用一个码是因为下一步是同一件事：重读一遍再决定。
* 路径逃逸、空路径、空字节、超长、目标是个目录 → 400。这些是参数不对，不是两个
  写者撞上了。
* 读一份不存在的、列一个不存在的目录 → 404。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from app.domain.world_documents import (
    WorldDocumentDeleteRequest,
    WorldDocumentListingRequest,
    WorldDocumentReadRequest,
    WorldDocumentWriteRequest,
)
from app.living import documents
from app.living.documents import ChangeOutcome
from app.runtime import node

_CONFLICT = 409
_BAD_ARGUMENT = 400
_NOT_THERE = 404


def _lane() -> str:
    """这次调用实际落在哪棵树上。

    **读的是进程自己的部署环境，请求里的任何东西都进不来。** 泳道不在注册表里时请求
    会静默落到 prod 的 pod 上并返回 200 —— 对调用方来说这跟送达成功长得一模一样，而
    这组接口的每一次写入都不可逆（这棵树没有版本历史，没有备份）。自报的落点是
    "这次改的是我以为的那棵树"的唯一证据，回显请求等于什么都没验。
    """
    return documents.documents_lane()


@contextmanager
def _arguments_that_do_not_work() -> Iterator[None]:
    """把"参数不对"翻成 4xx。

    文档层对这几种抛的是裸异常而不是 :class:`DocumentChange`，因为它们不是两个写者撞
    上了，是这一次调用本身就没法执行。所以它们不该拿冲突那个码 —— 拿了的话调用方会
    照着"重读一遍再来"去重试一条永远解析不出来的路径。
    """
    try:
        yield
    except (IsADirectoryError, NotADirectoryError) as exc:
        raise HTTPException(
            _BAD_ARGUMENT, detail={"lane": _lane(), "message": str(exc)}
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            _NOT_THERE, detail={"lane": _lane(), "message": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            _BAD_ARGUMENT, detail={"lane": _lane(), "message": str(exc)}
        ) from exc


def _landed_or_conflict(change: documents.DocumentChange) -> dict:
    """写成了就交回去；三种拒绝都是 409，靠 ``outcome`` 分辨是哪一种。"""
    if change.outcome is not ChangeOutcome.OK:
        raise HTTPException(
            _CONFLICT,
            detail={
                "lane": _lane(),
                "path": change.path,
                "outcome": str(change.outcome),
                "message": change.said,
            },
        )
    return {
        "lane": _lane(),
        "path": change.path,
        "outcome": str(change.outcome),
        "fingerprint": change.fingerprint,
    }


@node
async def world_document_listing_node(r: WorldDocumentListingRequest):
    with _arguments_that_do_not_work():
        found = await documents.listing(r.under)
    return {
        "lane": _lane(),
        "under": r.under,
        "mounted": found.mounted,
        "entries": list(found.entries),
    }


@node
async def world_document_read_node(r: WorldDocumentReadRequest):
    with _arguments_that_do_not_work():
        found = await documents.read_whole(r.path)
    return {
        "lane": _lane(),
        "path": found.path,
        "fingerprint": found.fingerprint,
        "content": found.content,
    }


@node
async def world_document_write_node(r: WorldDocumentWriteRequest):
    with _arguments_that_do_not_work():
        change = await documents.rewrite(r.path, r.content, r.fingerprint)
    return _landed_or_conflict(change)


@node
async def world_document_delete_node(r: WorldDocumentDeleteRequest):
    with _arguments_that_do_not_work():
        change = await documents.remove(r.path, r.fingerprint)
    return _landed_or_conflict(change)
