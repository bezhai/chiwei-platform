"""测试印象蒸馏：从描述性段落改为一句话感觉 gestalt"""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_gestalt_impression_is_short():
    """蒸馏后的印象应该是一句话（≤60字）"""
    fake_llm_response = json.dumps([
        {"user_id": "uid_001", "impression_text": "群里的指挥官，嘴硬心软，跟他互动很轻松"}
    ])

    mock_response = MagicMock(content=fake_llm_response)
    mock_model = MagicMock(ainvoke=AsyncMock(return_value=mock_response))

    with patch(
        "app.workers.diary_worker.ModelBuilder.build_chat_model",
        new_callable=AsyncMock,
        return_value=mock_model,
    ), patch(
        "app.workers.diary_worker.get_all_impressions_for_chat",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "app.workers.diary_worker.upsert_person_impression",
        new_callable=AsyncMock,
    ) as mock_upsert, patch(
        "app.workers.diary_worker.get_prompt",
        return_value=MagicMock(compile=MagicMock(return_value="compiled prompt")),
    ):
        from app.workers.diary_worker import post_process_impressions

        await post_process_impressions(
            chat_id="chat_001",
            diary_content="A哥今天又在组织角色分配，嘴硬心软的指挥官",
            user_names={"uid_001": "A哥"},
        )

        mock_upsert.assert_called_once()
        impression_text = mock_upsert.call_args[0][2]
        assert len(impression_text) <= 60, f"Impression too long: {len(impression_text)} chars"


@pytest.mark.asyncio
async def test_gestalt_skips_unknown_users():
    """蒸馏结果中不在 user_names 映射中的 user_id 应被跳过"""
    fake_llm_response = json.dumps([
        {"user_id": "uid_001", "impression_text": "群里的指挥官"},
        {"user_id": "uid_unknown", "impression_text": "不认识的人"},
    ])

    mock_response = MagicMock(content=fake_llm_response)
    mock_model = MagicMock(ainvoke=AsyncMock(return_value=mock_response))

    with patch(
        "app.workers.diary_worker.ModelBuilder.build_chat_model",
        new_callable=AsyncMock,
        return_value=mock_model,
    ), patch(
        "app.workers.diary_worker.get_all_impressions_for_chat",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "app.workers.diary_worker.upsert_person_impression",
        new_callable=AsyncMock,
    ) as mock_upsert, patch(
        "app.workers.diary_worker.get_prompt",
        return_value=MagicMock(compile=MagicMock(return_value="compiled prompt")),
    ):
        from app.workers.diary_worker import post_process_impressions

        await post_process_impressions(
            chat_id="chat_001",
            diary_content="A哥今天又在组织角色分配",
            user_names={"uid_001": "A哥"},
        )

        # Should only upsert for uid_001, not uid_unknown
        assert mock_upsert.call_count == 1
        assert mock_upsert.call_args[0][1] == "uid_001"


@pytest.mark.asyncio
async def test_gestalt_handles_empty_llm_response():
    """LLM 返回空列表时不应 crash"""
    mock_response = MagicMock(content="[]")
    mock_model = MagicMock(ainvoke=AsyncMock(return_value=mock_response))

    with patch(
        "app.workers.diary_worker.ModelBuilder.build_chat_model",
        new_callable=AsyncMock,
        return_value=mock_model,
    ), patch(
        "app.workers.diary_worker.get_all_impressions_for_chat",
        new_callable=AsyncMock,
        return_value=[],
    ), patch(
        "app.workers.diary_worker.upsert_person_impression",
        new_callable=AsyncMock,
    ) as mock_upsert, patch(
        "app.workers.diary_worker.get_prompt",
        return_value=MagicMock(compile=MagicMock(return_value="compiled prompt")),
    ):
        from app.workers.diary_worker import post_process_impressions

        await post_process_impressions(
            chat_id="chat_001",
            diary_content="今天群里很安静",
            user_names={"uid_001": "A哥"},
        )

        mock_upsert.assert_not_called()
