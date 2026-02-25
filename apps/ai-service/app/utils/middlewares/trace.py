"""
Header Context 中间件 - 统一管理请求头变量
"""

import contextvars
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# 存储所有header变量的容器
header_vars: dict[str, contextvars.ContextVar[Any]] = {}

# 预定义的header配置
HEADER_CONFIG = {
    "X-Trace-Id": {
        "var_name": "trace_id",
        "default_factory": lambda: str(uuid.uuid4()),
        "required": True,
    },
    "X-App-Name": {
        "var_name": "app_name",
        "default_factory": lambda: None,
        "required": False,
    },
}

# 初始化context变量
for _header_name, config in HEADER_CONFIG.items():
    var_name = config["var_name"]
    header_vars[var_name] = contextvars.ContextVar(var_name, default=None)


class HeaderContextMiddleware(BaseHTTPMiddleware):
    """
    Header Context 中间件

    功能：
    1. 从HTTP请求头中读取配置的header变量
    2. 为缺失的必需变量生成默认值
    3. 将所有变量存储到上下文供全局使用
    4. 在响应头中返回所有变量
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 处理所有配置的header
        for header_name, config in HEADER_CONFIG.items():
            var_name = config["var_name"]
            header_value = request.headers.get(header_name)

            # 如果没有值且是必需的，生成默认值
            if not header_value and config["default_factory"]:
                header_value = config["default_factory"]()

            # 存储到上下文变量
            header_vars[var_name].set(header_value)

        # 继续处理请求
        response = await call_next(request)

        # 在响应头中添加所有变量
        for header_name, config in HEADER_CONFIG.items():
            var_name = config["var_name"]
            value = header_vars[var_name].get()
            if value is not None:
                response.headers[header_name] = str(value)

        return response


def get_header_var(var_name: str) -> Any:
    """
    获取指定的header变量值

    Args:
        var_name: 变量名 (如 'trace_id', 'app_name')

    Returns:
        变量值，如果不存在则返回None
    """
    var = header_vars.get(var_name)
    return var.get() if var else None


def get_trace_id() -> str | None:
    """获取当前请求的traceId"""
    return get_header_var("trace_id")


def get_app_name() -> str | None:
    """获取当前请求的appId"""
    return get_header_var("app_name")
