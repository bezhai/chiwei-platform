"""
日志配置模块
"""

import logging
import sys

from .formatter import CustomJSONFormatter


def setup_logging():
    """
    设置日志配置

    配置JSON格式的日志输出，包含traceId字段
    """
    # 获取根logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 清除现有的handlers
    root_logger.handlers.clear()

    # 创建控制台handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # 设置JSON格式化器
    json_formatter = CustomJSONFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    console_handler.setFormatter(json_formatter)

    # 添加handler到根logger
    root_logger.addHandler(console_handler)

    # 设置uvicorn的logger
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers.clear()
    uvicorn_logger.addHandler(console_handler)
    uvicorn_logger.setLevel(logging.INFO)

    # 设置uvicorn.access的logger
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.handlers.clear()
    uvicorn_access_logger.addHandler(console_handler)
    uvicorn_access_logger.setLevel(logging.INFO)
