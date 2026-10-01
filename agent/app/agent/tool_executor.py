import json

from app.tool.registry import TOOLS


class ToolExecutor:

    def execute(
        self,
        tool_name: str,
        arguments: str,
    ) -> str:

        try:
            tool_args = json.loads(arguments)
        except json.JSONDecodeError:
            return "工具参数解析失败"

        tool = TOOLS.get(tool_name)

        if tool is None:
            return f"工具不存在：{tool_name}"

        try:
            result = tool(**tool_args)
            return str(result)

        except Exception as e:
            return f"工具执行失败：{e}"


tool_executor = ToolExecutor()