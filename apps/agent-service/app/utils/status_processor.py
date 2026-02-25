class AIMessageChunkProcessor:
    def __init__(self):
        self.TOOL_STATUS_MESSAGES: dict[str, str] = {
            "search_web": "让我来搜搜看~",
            "search_donjin_event": "哇！看起来跟同人有关系！让我找找！",
            "topic_summary": "让小尾回忆一下~",
            "bangumi_search": "让小尾查查Bangumi~",
            "generate_image": "小尾正在进行艺术创作~🖼️",
        }

        self.DEFAULT_STATUS_MESSAGES = {
            "thinking": "小尾正在努力思考...🤔",
            "replying": "小尾正在努力打字✍️",
            "tool_calling": "看我的独家秘技！✨",
        }

        self._current_state = "initial"

    def process_chunk(self, chunk) -> str | None:
        """处理AIMessageChunk，状态变化时返回提示消息，否则返回None"""

        if hasattr(chunk, "type"):
            print(f"Processing chunk of type: {chunk.type}")

        # 检查工具调用
        tool_calls = []
        if hasattr(chunk, "tool_calls") and chunk.tool_calls:
            tool_calls = chunk.tool_calls

        # 确定新状态
        if tool_calls:
            # 取第一个工具的名称
            first_tool = tool_calls[0]
            tool_name = first_tool.get("name")

            new_state = tool_name if tool_name else "tool_calling"
        elif hasattr(chunk, "text") and chunk.text and chunk.text.strip():
            new_state = "replying"
        else:
            new_state = "thinking"

        # 检查状态是否变化
        if new_state != self._current_state:
            self._current_state = new_state

            # 返回对应消息
            if new_state in self.TOOL_STATUS_MESSAGES:
                return self.TOOL_STATUS_MESSAGES[new_state]
            elif new_state in self.DEFAULT_STATUS_MESSAGES:
                return self.DEFAULT_STATUS_MESSAGES[new_state]
            else:
                return self.DEFAULT_STATUS_MESSAGES["tool_calling"]

        return None
