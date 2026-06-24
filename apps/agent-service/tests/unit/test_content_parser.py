"""Tests for content_parser module"""

import json

from app.chat.content_parser import parse_content, update_tos_files


class TestParseContentV2:
    """v2 JSON 格式解析"""

    def test_basic_v2_with_text_and_images(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "look at this: ![image](img_key) and [视频: clip.mp4]",
                "items": [
                    {"type": "text", "value": "look at this: "},
                    {"type": "image", "value": "img_key"},
                    {"type": "text", "value": " and "},
                    {
                        "type": "media",
                        "value": "file_key",
                        "meta": {"file_name": "clip.mp4"},
                    },
                ],
            }
        )
        result = parse_content(raw)
        assert result.text == "look at this: ![image](img_key) and [视频: clip.mp4]"
        assert result.image_keys == ["img_key"]
        assert len(result.items) == 4

    def test_v2_text_only(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "hello world",
                "items": [{"type": "text", "value": "hello world"}],
            }
        )
        result = parse_content(raw)
        assert result.text == "hello world"
        assert result.image_keys == []
        assert len(result.items) == 1

    def test_v2_multiple_images(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "![image](img1)![image](img2)",
                "items": [
                    {"type": "image", "value": "img1"},
                    {"type": "image", "value": "img2"},
                ],
            }
        )
        result = parse_content(raw)
        assert result.image_keys == ["img1", "img2"]

    def test_v2_empty_items(self):
        raw = json.dumps({"v": 2, "text": "", "items": []})
        result = parse_content(raw)
        assert result.text == ""
        assert result.image_keys == []
        assert result.items == []

    def test_v2_missing_text_field(self):
        raw = json.dumps({"v": 2, "items": [{"type": "text", "value": "hello"}]})
        result = parse_content(raw)
        assert result.text == ""
        assert result.items == [{"type": "text", "value": "hello"}]

    def test_v2_with_non_image_items_only(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[文件: doc.pdf]",
                "items": [
                    {
                        "type": "file",
                        "value": "file_key",
                        "meta": {"file_name": "doc.pdf"},
                    }
                ],
            }
        )
        result = parse_content(raw)
        assert result.text == "[文件: doc.pdf]"
        assert result.image_keys == []
        assert len(result.items) == 1


class TestParseContentNonV2Fallback:
    """非 v2 格式降级为纯文本"""

    def test_plain_text(self):
        result = parse_content("hello world")
        assert result.text == "hello world"
        assert result.image_keys == []
        assert result.items == []

    def test_empty_string(self):
        result = parse_content("")
        assert result.text == ""
        assert result.image_keys == []
        assert result.items == []

    def test_invalid_json(self):
        result = parse_content("{invalid json")
        assert result.text == "{invalid json"
        assert result.image_keys == []

    def test_json_without_v_field(self):
        raw = json.dumps({"text": "hello", "items": []})
        result = parse_content(raw)
        # 非 v2，降级为纯文本（原始 JSON 字符串）
        assert result.text == raw
        assert result.image_keys == []
        assert result.items == []

    def test_json_with_wrong_v(self):
        raw = json.dumps({"v": 1, "text": "hello"})
        result = parse_content(raw)
        assert result.text == raw
        assert result.image_keys == []
        assert result.items == []

    def test_json_array(self):
        raw = json.dumps([1, 2, 3])
        result = parse_content(raw)
        assert result.text == raw
        assert result.image_keys == []
        assert result.items == []

    def test_markdown_with_image_syntax_not_extracted(self):
        """v1 格式的图片标记不再被提取，直接作为纯文本"""
        raw = "look at this: ![image](img_key) nice"
        result = parse_content(raw)
        assert result.text == raw
        assert result.image_keys == []
        assert result.items == []


class TestRender:
    """render() 结构化渲染"""

    def test_render_text_only(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "hello world",
                "items": [{"type": "text", "value": "hello world"}],
            }
        )
        result = parse_content(raw)
        assert result.render() == "hello world"

    def test_render_skips_images_by_default(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "before ![image](key1) after",
                "items": [
                    {"type": "text", "value": "before "},
                    {"type": "image", "value": "key1"},
                    {"type": "text", "value": " after"},
                ],
            }
        )
        result = parse_content(raw)
        assert result.render() == "before [图片] after"

    def test_render_with_image_fn(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "看 ![image](k1) 和 ![image](k2)",
                "items": [
                    {"type": "text", "value": "看 "},
                    {"type": "image", "value": "k1"},
                    {"type": "text", "value": " 和 "},
                    {"type": "image", "value": "k2"},
                ],
            }
        )
        result = parse_content(raw)
        rendered = result.render(image_fn=lambda i, key: f"【图片{i + 1}】")
        assert rendered == "看 【图片1】 和 【图片2】"

    def test_render_with_offset_image_fn(self):
        """群聊场景：图片编号从 start_index 开始"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "![image](k1)",
                "items": [{"type": "image", "value": "k1"}],
            }
        )
        result = parse_content(raw)
        start = 3
        rendered = result.render(image_fn=lambda i, _k: f"【图片{start + i + 1}】")
        assert rendered == "【图片4】"

    def test_render_media_with_filename(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[视频: clip.mp4]",
                "items": [
                    {
                        "type": "media",
                        "value": "file_key",
                        "meta": {"file_name": "clip.mp4"},
                    }
                ],
            }
        )
        result = parse_content(raw)
        assert result.render() == "[视频: clip.mp4]"

    def test_render_media_without_filename(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[视频]",
                "items": [{"type": "media", "value": "file_key"}],
            }
        )
        result = parse_content(raw)
        assert result.render() == "[视频]"

    def test_render_file(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[文件: doc.pdf]",
                "items": [
                    {
                        "type": "file",
                        "value": "file_key",
                        "meta": {"file_name": "doc.pdf"},
                    }
                ],
            }
        )
        result = parse_content(raw)
        assert result.render() == "[文件: doc.pdf]"

    def test_render_audio(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[语音]",
                "items": [{"type": "audio", "value": "audio_key"}],
            }
        )
        result = parse_content(raw)
        assert result.render() == "[语音]"

    def test_render_sticker(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[表情包]",
                "items": [{"type": "sticker", "value": "sticker_key"}],
            }
        )
        result = parse_content(raw)
        assert result.render() == "[表情包]"

    def test_render_unsupported(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[不支持的消息]",
                "items": [{"type": "unsupported", "value": "[不支持的消息]"}],
            }
        )
        result = parse_content(raw)
        assert result.render() == "[不支持的消息]"

    def test_render_mixed_content(self):
        """完整混合内容场景"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "hello ![image](img1) [视频: v.mp4] [文件: f.pdf]",
                "items": [
                    {"type": "text", "value": "hello "},
                    {"type": "image", "value": "img1"},
                    {"type": "text", "value": " "},
                    {
                        "type": "media",
                        "value": "media_key",
                        "meta": {"file_name": "v.mp4"},
                    },
                    {"type": "text", "value": " "},
                    {
                        "type": "file",
                        "value": "file_key",
                        "meta": {"file_name": "f.pdf"},
                    },
                ],
            }
        )
        result = parse_content(raw)
        # 默认图片渲染为 [图片]
        assert result.render() == "hello [图片] [视频: v.mp4] [文件: f.pdf]"
        # 带图片标记
        assert (
            result.render(image_fn=lambda i, k: f"[IMG:{k}]")
            == "hello [IMG:img1] [视频: v.mp4] [文件: f.pdf]"
        )

    def test_render_empty_items_falls_back_to_text(self):
        """items 为空时回退到 text 字段"""
        raw = json.dumps({"v": 2, "text": "fallback text", "items": []})
        result = parse_content(raw)
        assert result.render() == "fallback text"

    def test_mentions_parsed_from_v2(self):
        """v2 格式解析 mentions 用户信息"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "@杜中成 每日一图",
                "items": [
                    {"type": "text", "value": "@杜中成 每日一图"},
                ],
                "mentions": [{"user_id": "ou_xxx", "name": "杜中成"}],
            }
        )
        result = parse_content(raw)
        assert result.mentions == [{"user_id": "ou_xxx", "name": "杜中成"}]
        assert result.render() == "@杜中成 每日一图"

    def test_mentions_multiple_users(self):
        """多个 @mention 用户信息都保留"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "@张三 @李四 你们好",
                "items": [
                    {"type": "text", "value": "@张三 @李四 你们好"},
                ],
                "mentions": [
                    {"user_id": "ou_aaa", "name": "张三"},
                    {"user_id": "ou_bbb", "name": "李四"},
                ],
            }
        )
        result = parse_content(raw)
        assert len(result.mentions) == 2
        assert result.mentions[0]["name"] == "张三"
        assert result.mentions[1]["user_id"] == "ou_bbb"
        assert result.render() == "@张三 @李四 你们好"

    def test_render_mention_items(self):
        """v2 mention item 渲染为平台无关 @展示名"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "@张三 你好",
                "items": [
                    {"type": "mention", "value": "张三"},
                    {"type": "text", "value": " 你好"},
                ],
                "mentions": [{"user_id": "u_common", "name": "张三"}],
            }
        )
        result = parse_content(raw)
        assert result.render() == "@张三 你好"

    def test_no_mentions_field(self):
        """无 mentions 字段时默认空列表"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "@杜中成 每日一图",
                "items": [
                    {"type": "text", "value": "@杜中成 每日一图"},
                ],
            }
        )
        result = parse_content(raw)
        assert result.mentions == []
        assert result.render() == "@杜中成 每日一图"

    def test_render_non_v2_returns_raw(self):
        """非 v2 输入，render() 返回原始文本"""
        result = parse_content("plain text")
        assert result.render() == "plain text"


class TestFileAttachmentReference:
    """文件内容项携带对象存储引用（读小说 Task 1 契约）。

    文件项的"附件实例身份"是隐式的：它就是这条消息里那个位置上的文件项。对象存储
    引用是挂在该文件项上的 ``tos_file`` 字段（和图片同款），承载这次实例的字节载荷。
    """

    def test_parse_exposes_file_keys(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[文件: 斜阳.txt]",
                "items": [
                    {
                        "type": "file",
                        "value": "file_k1",
                        "meta": {"file_name": "斜阳.txt"},
                    }
                ],
            }
        )
        result = parse_content(raw)
        assert result.file_keys == ["file_k1"]

    def test_parse_exposes_file_tos_reference(self):
        """已回填的文件项 tos_file 进 tos_files 映射（按 file_key）。"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "[文件: 斜阳.txt]",
                "items": [
                    {
                        "type": "file",
                        "value": "file_k1",
                        "meta": {"file_name": "斜阳.txt"},
                        "tos_file": "files/file_k1",
                    }
                ],
            }
        )
        result = parse_content(raw)
        assert result.file_keys == ["file_k1"]
        assert result.tos_files.get("file_k1") == "files/file_k1"

    def test_update_tos_files_enriches_file_item(self):
        """update_tos_files 把对象存储引用写进文件项（先有项、后回填引用）。"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "[文件: 斜阳.txt]",
                "items": [
                    {
                        "type": "file",
                        "value": "file_k1",
                        "meta": {"file_name": "斜阳.txt"},
                    }
                ],
            }
        )
        updated = update_tos_files(raw, {"file_k1": "files/file_k1"})
        assert updated is not None
        data = json.loads(updated)
        file_item = data["items"][0]
        assert file_item["type"] == "file"
        assert file_item["tos_file"] == "files/file_k1"
        # identity payload untouched: still the same file_key + name
        assert file_item["value"] == "file_k1"
        assert file_item["meta"]["file_name"] == "斜阳.txt"

    def test_update_tos_files_noop_when_already_set(self):
        raw = json.dumps(
            {
                "v": 2,
                "text": "[文件: 斜阳.txt]",
                "items": [
                    {
                        "type": "file",
                        "value": "file_k1",
                        "meta": {"file_name": "斜阳.txt"},
                        "tos_file": "files/file_k1",
                    }
                ],
            }
        )
        assert update_tos_files(raw, {"file_k1": "files/file_k1"}) is None

    def test_image_and_file_both_enriched_independently(self):
        """同一条消息里图片项和文件项各自按自己的 key 回填，互不串。"""
        raw = json.dumps(
            {
                "v": 2,
                "text": "![image](img_k) [文件: a.txt]",
                "items": [
                    {"type": "image", "value": "img_k"},
                    {"type": "file", "value": "file_k", "meta": {"file_name": "a.txt"}},
                ],
            }
        )
        updated = update_tos_files(
            raw, {"img_k": "temp/img_k.jpg", "file_k": "files/file_k"}
        )
        assert updated is not None
        data = json.loads(updated)
        assert data["items"][0]["tos_file"] == "temp/img_k.jpg"
        assert data["items"][1]["tos_file"] == "files/file_k"
