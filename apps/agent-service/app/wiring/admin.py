"""Admin / public-API HTTP wiring — Phase 6 v4 Gap 1 closure.

Each wire declares a Source.http(...) input + admin node consumer; runtime
auto-registers FastAPI routes via register_http_sources(app).

All endpoints use response=True (RPC mode) to preserve the old synchronous
response shapes — clients depend on getting a JSON body back, not a 202.
"""
from app.domain.admin import AdminSearchRequest
from app.nodes.admin import admin_search_node
from app.runtime import Source, wire

# Admin trigger endpoints — all RPC mode (preserve old sync response shape).
wire(AdminSearchRequest).from_(
    Source.http("/admin/search", response=True)
).to(admin_search_node)

# Phase 7b Gap 12: DLQ admin endpoints.
from app.domain.dlq_admin_events import (  # noqa: E402
    DlqClearIdempotentRequest,
    DlqDryRunRequest,
    DlqInspectRequest,
    DlqRequeueRequest,
)
from app.nodes.dlq_admin import (  # noqa: E402
    dlq_clear_idempotent_node,
    dlq_dry_run_node,
    dlq_inspect_node,
    dlq_requeue_node,
)

wire(DlqInspectRequest).from_(
    Source.http("/admin/dlq/inspect", method="POST", response=True)
).to(dlq_inspect_node)
wire(DlqClearIdempotentRequest).from_(
    Source.http("/admin/dlq/clear-idempotent", method="POST", response=True)
).to(dlq_clear_idempotent_node)
wire(DlqDryRunRequest).from_(
    Source.http("/admin/dlq/dry-run", method="POST", response=True)
).to(dlq_dry_run_node)
wire(DlqRequeueRequest).from_(
    Source.http("/admin/dlq/requeue", method="POST", response=True)
).to(dlq_requeue_node)

# 世界文档树的外部端点 —— 列目录 / 读一份 / 整份重写 / 删掉。
#
# 一个资源（document）三个方法，加上一条列目录。路径里放不下文档路径（它自己带
# ``/``），所以走查询串和 body。
#
# **没有一条路由能指定泳道**：树的根是 ``$WORLD_DOCS_DIR/<泳道>``，泳道那一段由收到
# 请求的进程按自己的部署环境拼。选哪棵树靠请求被路由到哪个 pod。
#
# **四条都要凭据**（``INNER_HTTP_SECRET`` + ``Authorization: Bearer``，见
# :mod:`app.runtime.http_auth`）。这个服务的路由前缀整段对外可达，而这里面有一条能
# 删掉一份文档 —— 那份文档的正文是原样塞进模型眼前的，删掉之后她走进那个地方什么都
# 看不到，且不会有任何报错。上面那几条运维口今天是裸的，最坏是重投一批死信，不是同一
# 个量级；这次加的是这四条的门，不是把整段前缀关起来。
from app.domain.world_documents import (  # noqa: E402
    WorldDocumentDeleteRequest,
    WorldDocumentListingRequest,
    WorldDocumentReadRequest,
    WorldDocumentWriteRequest,
)
from app.nodes.world_documents import (  # noqa: E402
    world_document_delete_node,
    world_document_listing_node,
    world_document_read_node,
    world_document_write_node,
)

wire(WorldDocumentListingRequest).from_(
    Source.http(
        "/admin/world-documents/listing",
        method="GET",
        response=True,
        requires_inner_secret=True,
        answers_with_lane=True,
    )
).to(world_document_listing_node)
wire(WorldDocumentReadRequest).from_(
    Source.http(
        "/admin/world-documents/document",
        method="GET",
        response=True,
        requires_inner_secret=True,
        answers_with_lane=True,
    )
).to(world_document_read_node)
wire(WorldDocumentWriteRequest).from_(
    Source.http(
        "/admin/world-documents/document",
        method="PUT",
        response=True,
        requires_inner_secret=True,
        answers_with_lane=True,
    )
).to(world_document_write_node)
wire(WorldDocumentDeleteRequest).from_(
    Source.http(
        "/admin/world-documents/document",
        method="DELETE",
        response=True,
        requires_inner_secret=True,
        answers_with_lane=True,
    )
).to(world_document_delete_node)
