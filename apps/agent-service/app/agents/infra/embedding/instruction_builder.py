"""Embedding instructions 构建器"""

from app.agents.infra.embedding.modality import Modality


class InstructionBuilder:
    """Embedding instructions 构建器

    根据 doubao-embedding-vision 模型文档，instructions 字段的配置规则：

    1. 召回/排序类任务（区分 Query/Corpus）：
       - Query 侧: Target_modality: {}.\nInstruction:{}\nQuery:
       - Corpus 侧: Instruction:Compress the {} into one word.\nQuery:

    2. 聚类/分类/STS 类任务（不区分）：
       - 所有数据: Target_modality: {}.\nInstruction:{}\nQuery:
    """

    @staticmethod
    def detect_input_modality(
        text: str | None,
        images: list[str] | None,
    ) -> str:
        """
        检测单条输入的模态（用于 Corpus 侧 / 聚类场景）

        根据输入内容自动判断：
        - 有文本有图片 -> "text and image"
        - 只有文本 -> "text"
        - 只有图片 -> "image"

        Args:
            text: 文本内容
            images: 图片列表

        Returns:
            模态字符串
        """
        has_text = bool(text and text.strip())
        has_image = bool(images)

        if has_text and has_image:
            return Modality.TEXT_AND_IMAGE
        elif has_text:
            return Modality.TEXT
        elif has_image:
            return Modality.IMAGE
        return Modality.TEXT  # fallback

    @staticmethod
    def combine_corpus_modalities(*modalities: str) -> str:
        """
        组合 Corpus 库包含的多种模态类型（用于 Query 侧 Target_modality）

        用 `/` 分隔表示库中存在这些独立类型的样本

        Args:
            modalities: 模态类型列表

        Returns:
            组合后的模态字符串

        Examples:
            combine_corpus_modalities("text", "image")
                -> "text/image"
            combine_corpus_modalities("text", "image", "text and image")
                -> "text/image/text and image"
        """
        return "/".join(modalities)

    @staticmethod
    def for_corpus(modality: str) -> str:
        """
        Corpus 侧 instructions（召回/排序任务）

        Args:
            modality: 当前单条数据的模态

        Returns:
            instructions 字符串
        """
        return f"Instruction:Compress the {modality} into one word.\nQuery:"

    @staticmethod
    def for_query(target_modality: str, instruction: str) -> str:
        """
        Query 侧 instructions（召回/排序任务）

        Args:
            target_modality: Corpus 库的模态类型（用 / 分隔多种类型）
            instruction: 检索意图描述

        Returns:
            instructions 字符串
        """
        return f"Target_modality: {target_modality}.\nInstruction:{instruction}\nQuery:"

    @staticmethod
    def for_cluster(target_modality: str, instruction: str) -> str:
        """
        聚类/分类/STS 类 instructions

        Args:
            target_modality: 数据集的统一模态类型
            instruction: 任务描述

        Returns:
            instructions 字符串
        """
        return f"Target_modality: {target_modality}.\nInstruction:{instruction}\nQuery:"
