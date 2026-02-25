"""LangGraph基础设施异常定义"""


class ModelBuilderError(Exception):
    """ModelBuilder相关异常基类"""

    pass


class UnsupportedModelError(ModelBuilderError):
    """不支持的模型类型异常"""

    def __init__(self, model_id: str, message: "str | None" = None):
        self.model_id = model_id
        if message is None:
            message = f"不支持的模型: {model_id}"
        super().__init__(message)


class ModelConfigError(ModelBuilderError):
    """模型配置错误异常"""

    def __init__(self, model_id: str, message: "str | None" = None):
        self.model_id = model_id
        if message is None:
            message = f"模型配置错误: {model_id}"
        super().__init__(message)


class BannedWordError(Exception):
    """消息包含封禁词异常"""

    def __init__(self, word: str, message: "str | None" = None):
        self.word = word
        if message is None:
            message = f"消息包含封禁词: {word}"
        super().__init__(message)
