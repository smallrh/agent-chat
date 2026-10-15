from pathlib import Path
p = Path(r"D:\work\agent_system\backend\cmd\server\main.go")
t = p.read_text(encoding="utf-8")
t = t.replace(
    "conversationHandler := handler.NewConversationHandler(agentClient)\n\t\t\tr := router.New",
    "conversationHandler := handler.NewConversationHandler(agentClient)\n\t\tr := router.New",
)
p.write_text(t, encoding="utf-8")
print("fixed")
