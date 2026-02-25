"""共享类型定义"""

from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
ClientT = TypeVar("ClientT")
TConfig = TypeVar("TConfig")

# 通用类型别名
JsonDict = dict[str, Any]
