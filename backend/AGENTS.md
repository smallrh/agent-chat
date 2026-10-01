# Go Authentication Service 开发规则

本项目是一个独立部署、独立运行的 Go Authentication Service。
## 配置了热更新，不要启动我的代码，我的代码是一直启动着的（.air.toml）
在修改任何代码之前，必须先阅读：

- `.agent/rules/00-项目总则.md`
- `.agent/rules/01-Go编码规范.md`
- `.agent/rules/02-项目架构规范.md`

根据任务类型继续阅读对应规则：

数据库相关：
- `.agent/rules/03-数据库规范.md`

认证、安全、JWT、密码、Refresh Token：
- `.agent/rules/04-认证安全规范.md`

API、HTTP 接口：
- `.agent/rules/05-API规范.md`

错误处理：
- `.agent/rules/06-错误处理规范.md`

测试：
- `.agent/rules/07-测试规范.md`

性能：
- `.agent/rules/08-性能规范.md`

日志、Request ID、健康检查：
- `.agent/rules/09-日志与可观测性.md`

Go → Python Agent：
- `.agent/rules/10-Agent代理规范.md`

跨服务 API 契约（重要！）：
- `.agent/rules/12-跨服务契约规范.md`

Python Agent 服务开发：
- `.agent/rules/13-Python Agent 服务规范.md`

AI Coding Agent：
- `.agent/rules/11-AI编码规范.md`

任务涉及跨服务时，**必读 12**。

---

# 不可违反的核心安全边界

客户端：

Authorization = User A
X-User-Id = User B

Go Auth Service：

1. 验证 Authorization 中的 JWT。
2. 从 JWT.sub 获取 User A。
3. 忽略客户端传入的 X-User-Id。
4. 重新设置 X-User-Id = User A。
5. 转发给 Python Agent。

最终：

Python Agent
↓
X-User-Id = User A

绝对不能出现：

Python Agent
↓
X-User-Id = User B

---

# 不可违反的身份链路

Go Auth 数据库: auth_users.auth_user_uuid
           ↓
JWT Claims: sub = user_uuid (UUID)
           ↓
AuthContext (Go): UserUUID
           ↓
Go → Python: X-User-Id = user_uuid (UUID string)
           ↓
Python Agent: 从 X-User-Id Header 读 user_uuid → 转换为 users.id (int8) → 内部使用

**两个注意点**：

1. `auth_users.id`（Go 自增主键 int64）**绝对不能**作为跨服务身份
2. `users.id`（Python 自增主键 int8）Go Auth **拿不到也不应该拿**

详见 `.agent/rules/12-跨服务契约规范.md`。

---

# 服务职责边界

Go Auth Service：

- 用户注册
- 用户登录
- 密码验证
- JWT
- Access Token
- Refresh Token
- Token Rotation
- Token Revocation
- 用户身份
- Authentication Middleware
- Request ID
- Rate Limit
- Agent Proxy

Python Agent Service：

- Agent
- Conversation
- Memory
- RAG
- Tool
- LLM
- 业务逻辑
- 业务数据权限判断

禁止将两个服务的职责混合。