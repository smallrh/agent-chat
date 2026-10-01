from app.tool.weather import weather


TOOLS = {
    "weather": weather,
}


TOOL_DEFINITIONS = [
        {
        "type": "function",
        "function": {
            "name": "weather",
            "description": (
                "查询指定城市的天气。"
                "支持当前天气、今天和未来7天天气。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，例如广州、深圳、北京",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": [
                            "current",
                            "today",
                            "week",
                        ],
                        "description": (
                            "天气时间范围："
                            "current=当前，"
                            "today=今天，"
                            "week=未来7天"
                        ),
                    },
                },
                "required": [
                    "city",
                    "time_range",
                ],
            },
        },
    },
]