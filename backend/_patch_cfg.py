from pathlib import Path

p = Path(r"D:\work\agent_system\backend\internal\config\config.go")
t = p.read_text(encoding="utf-8")
old = """\tRegisterIPLimit  int
\tRegisterIPWindow time.Duration

\t// 密钥自动生成目录
"""
new = """\tRegisterIPLimit  int
\tRegisterIPWindow time.Duration
\tChatUserLimit    int
\tChatUserWindow   time.Duration
\tSSETimeout       time.Duration

\t// 密钥自动生成目录
"""
if old not in t:
    raise SystemExit("struct block not found")
t = t.replace(old, new, 1)
old = """\t\tAgentTimeout:    getEnvDuration("AGENT_TIMEOUT", 30*time.Second),
"""
new = """\t\tAgentTimeout:    getEnvDuration("AGENT_TIMEOUT", 30*time.Second),
\t\tSSETimeout:      getEnvDuration("AGENT_SSE_TIMEOUT", 10*time.Minute),
"""
if old not in t:
    raise SystemExit("AgentTimeout load not found")
t = t.replace(old, new, 1)
old = """\t\tRegisterIPWindow: getEnvDuration("REGISTER_IP_WINDOW", 1*time.Minute),
"""
new = """\t\tRegisterIPWindow: getEnvDuration("REGISTER_IP_WINDOW", 1*time.Minute),
\t\tChatUserLimit:    getEnvInt("CHAT_USER_LIMIT", 30),
\t\tChatUserWindow:   getEnvDuration("CHAT_USER_WINDOW", 1*time.Minute),
"""
if old not in t:
    raise SystemExit("register window not found")
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8")
print("config ok")
