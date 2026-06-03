from app.data.queries import messages


def test_common_mention_item_to_v2_preserves_structure():
    item = {
        "kind": "mention",
        "id": "on_user",
        "label": "Alice",
        "meta": {"union_id": "on_user", "open_id": "ou_user"},
    }

    assert messages._content_item_to_v2(item) == {
        "type": "mention",
        "value": "Alice",
        "meta": {"union_id": "on_user", "open_id": "ou_user", "id": "on_user"},
    }


def test_common_mention_item_contributes_to_content_text():
    content = [
        {"kind": "mention", "id": "on_user", "label": "Alice"},
        {"kind": "text", "text": " hello "},
        {"type": "mention", "value": "Bob"},
    ]

    assert messages._content_text(content, None) == "@Alice hello @Bob"
