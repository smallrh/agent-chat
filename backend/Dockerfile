# Go Auth Service - Dockerfile
# Build stage
FROM golang:1.26-alpine AS builder

WORKDIR /app

# 安装 git（某些 go 依赖可能需要）
RUN apk add --no-cache git ca-certificates

# 先拷贝 go.mod / go.sum 缓存依赖
COPY go.mod go.sum ./
RUN go mod download

# 拷贝源码
COPY . .

# 构建
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-w -s" \
    -o /auth-service \
    ./cmd/server

# 运行阶段
FROM alpine:3.20

# 创建非 root 用户
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app

# 拷贝二进制
COPY --from=builder /auth-service /app/auth-service

# 创建 keys 目录（首次启动自动生成）
RUN mkdir -p /app/keys && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -qO- http://localhost:8080/health || exit 1

ENTRYPOINT ["/app/auth-service"]
