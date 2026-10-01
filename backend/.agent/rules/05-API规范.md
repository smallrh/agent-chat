# API 规范

## 1. API 前缀

统一：

/api/v1

---

## 2. Authentication API

POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
GET  /api/v1/auth/me

---

## 3. 健康检查

GET /health

表示：

进程正常运行。

GET /ready

表示：

数据库、Redis 等关键依赖已经准备完成。

---

## 4. Register

请求：

{
"username": "test",
"email": "test@example.com",
"password": "******"
}

响应禁止：

password
password_hash

---

## 5. Login

返回：

access_token
refresh_token
token_type
expires_in
user

---

## 6. /me

用户身份只能来自：

JWT → AuthContext

禁止：

/me?user_uuid=xxx

也禁止从 Request Body 获取当前用户身份。

---

## 7. HTTP Status

使用：

400 Bad Request
401 Unauthorized
403 Forbidden
404 Not Found
409 Conflict
429 Too Many Requests
500 Internal Server Error
502 Bad Gateway
504 Gateway Timeout

---

## 8. 错误响应

统一：

{
"code": "INVALID_CREDENTIALS",
"message": "Invalid username or password",
"request_id": "..."
}

禁止返回：

- SQL Error
- Stack Trace
- Python Exception
- 数据库连接信息
- 内部 URL