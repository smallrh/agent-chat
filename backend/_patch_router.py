from pathlib import Path

router = Path(r"D:\work\agent_system\backend\internal\router\router.go")
t = router.read_text(encoding="utf-8")
old = """\t\t\tauthProtected.POST("/logout", authHandler.Logout)
\t\t\tauthProtected.GET("/me", authHandler.Me)
\t\t}
\t}
"""
new = """\t\t\tauthProtected.POST("/logout", authHandler.Logout)
\t\t\tauthProtected.GET("/me", authHandler.Me)
\t\t}

\t\tconv := v1.Group("/conversations")
\t\tconv.Use(middleware.AuthMiddleware(tokenSvc))
\t\t{
\t\t\tconv.GET("", conversationHandler.List)
\t\t\tconv.GET("/:id/messages", conversationHandler.ListMessages)
\t\t\tconv.PATCH("/:id", conversationHandler.Rename)
\t\t\tconv.DELETE("/:id", conversationHandler.Delete)
\t\t}
\t}
"""
if old not in t:
    raise SystemExit("router block not found")
router.write_text(t.replace(old, new, 1), encoding="utf-8")
print("router ok")

main = Path(r"D:\work\agent_system\backend\cmd\server\main.go")
t = main.read_text(encoding="utf-8")
old = "r := router.New(authHandler, healthHandler, tokenSvc, agentClient, rateLimitSvc)"
new = "conversationHandler := handler.NewConversationHandler(agentClient)\n\t\tr := router.New(authHandler, healthHandler, tokenSvc, agentClient, rateLimitSvc, conversationHandler)"
if old not in t:
    raise SystemExit("main.go New() not found")
main.write_text(t.replace(old, new, 1), encoding="utf-8")
print("main ok")

integ = Path(r"D:\work\agent_system\backend\tests\integration_test.go")
t = integ.read_text(encoding="utf-8")
old = "testRouter = router.New(authHandler, healthHandler, tokenSvc, agentClient, rlSvc)"
new = "testRouter = router.New(authHandler, healthHandler, tokenSvc, agentClient, rlSvc, handler.NewConversationHandler(agentClient))"
if old not in t:
    raise SystemExit("integration_test not found")
integ.write_text(t.replace(old, new, 1), encoding="utf-8")
print("test ok")
