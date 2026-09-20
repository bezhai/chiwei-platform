"""从外面读写世界文档树的四个请求。

都是 transient：一次请求的参数，没有对应的 pg 表。

**这四个类里没有泳道字段，而且不能有。** 树的根是 ``$WORLD_DOCS_DIR/<泳道>``，泳道
那一段由收到请求的进程按自己的部署环境拼出来（:func:`app.living.documents.documents_root`）。
选哪棵树靠的是请求被路由到哪个 pod，不是参数。``Data`` 的 ``extra="forbid"`` 让这件事
在结构上成立：请求里带一个 ``lane`` 会被当成多余字段拒掉，而不是靠 handler 记得别去读它。

理由沿用文档树现有的那一条：靠参数选泳道的话，一次写错就直接改到 prod 的设定集，
而这件事没有报错，也没有备份可以退回去。
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class WorldDocumentListingRequest(Data):
    """列目录。``under`` 留空 = 整棵树。"""

    under: Annotated[str, Key] = ""

    class Meta:
        transient = True


class WorldDocumentReadRequest(Data):
    """读一份文档的全文和指纹。"""

    path: Annotated[str, Key]

    class Meta:
        transient = True


class WorldDocumentWriteRequest(Data):
    """整份重写。覆盖一份已经存在的文档要带上读到的那一版的指纹；新建一份留空。"""

    path: Annotated[str, Key]
    content: str
    fingerprint: str = ""

    class Meta:
        transient = True


class WorldDocumentDeleteRequest(Data):
    """删掉一份文档。指纹跟整份重写同一条规矩，不能比它松。"""

    path: Annotated[str, Key]
    fingerprint: str = ""

    class Meta:
        transient = True
