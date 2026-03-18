"""Research Agent - 深度调研子 Agent

工具集默认继承主 Agent（排除委派工具），通过独立的 prompt 聚焦调研任务。
"""

from app.agents.core.sub_agent import SubAgent

# 不指定 tools → 运行时自动取 BASE_TOOLS
research_agent = SubAgent("research")
