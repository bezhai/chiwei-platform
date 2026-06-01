from uuid import uuid4

from app.data.models import (
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
    CommonUser,
)


def test_common_models_are_registered_on_common_tables():
    assert CommonUser.__tablename__ == "common_user"
    assert CommonConversation.__tablename__ == "common_conversation"
    assert CommonMessage.__tablename__ == "common_message"
    assert CommonAgentResponse.__tablename__ == "common_agent_response"


def test_common_message_has_only_common_identity_columns():
    column_names = set(CommonMessage.__table__.columns.keys())

    assert "common_message_id" in column_names
    assert "common_conversation_id" in column_names
    assert "common_user_id" in column_names
    assert "om_id" not in column_names
    assert "chat_id" not in column_names
    assert "open_id" not in column_names


def test_common_agent_response_uses_common_reply_ids():
    response = CommonAgentResponse(
        response_id=uuid4(),
        session_id="s1",
        trigger_common_message_id=uuid4(),
        common_conversation_id=uuid4(),
        replies=[{"common_message_id": str(uuid4()), "content_type": "post"}],
    )

    assert response.replies[0]["common_message_id"]
    assert "message_id" not in response.replies[0]
