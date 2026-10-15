from pathlib import Path
t = Path(r"D:\work\agent_system\backend\internal\router\router.go").read_text(encoding="utf-8")
i = t.find("authHandler.Logout")
print(repr(t[i-120:i+250]))
