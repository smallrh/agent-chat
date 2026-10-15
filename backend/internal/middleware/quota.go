package middleware

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"agent/auth-service/internal/config"
	"agent/auth-service/internal/model"
	"agent/auth-service/internal/service"
)

// QuotaMiddleware 配额拦截（P3 计费）
//
// 与 ChatGuardMiddleware 的分工：
//   - ChatGuard：管**瞬时**（每分钟多少次 / 同时几条流）—— 防打爆连接池
//   - Quota：    管**总量**（今天还能用多少次）—— 计费
//   两者互补，都要有：只有 Quota 挡不住「一分钟打 200 次」把服务冲垮，
//   只有 ChatGuard 挡不住「慢慢用一整天」把额度烧光。
//
// 只对真正消耗成本的路径生效，列表/查询类请求不消耗配额。
func QuotaMiddleware(quotaSvc *service.QuotaService, cfg *config.Config) gin.HandlerFunc {
	return func(c *gin.Context) {
		// 未注入计费依赖（如集成测试）→ 直接放行，不改变任何行为
		if quotaSvc == nil || cfg == nil || !cfg.QuotaEnabled {
			c.Next()
			return
		}

		metric, ok := metricForPath(c)
		if !ok {
			c.Next() // 不消耗配额的路径直接放行
			return
		}

		authCtx := GetAuthContext(c)
		if authCtx == nil || authCtx.UserUUID == "" {
			// 没有身份就不判（AuthMiddleware 会先拦下 401）
			c.Next()
			return
		}

		if err := quotaSvc.CheckMetric(c.Request.Context(), authCtx.UserUUID, metric); err != nil {
			c.AbortWithStatusJSON(http.StatusTooManyRequests, model.ErrorResponse{
				Code:      "QUOTA_EXCEEDED",
				Message:   "今日" + metricLabel(metric) + "额度已用完，请明天再试或联系管理员调整套餐",
				RequestID: GetRequestID(c),
			})
			return
		}

		c.Next()
	}
}

// metricForPath 判断请求属于哪个计费维度。
// 返回 (metric, 是否计费)。
func metricForPath(c *gin.Context) (string, bool) {
	path := c.FullPath()
	if path == "" {
		// FullPath 为空时（未匹配到路由）退回原始路径判断
		path = c.Request.URL.Path
	}
	p := strings.TrimPrefix(path, "/")
	method := c.Request.Method

	// 对话：/agent/chat* 与 /api/v1/... 下的 chat（当前只有 /agent/chat*）
	if strings.HasPrefix(p, "agent/chat") {
		return service.MetricChat, true
	}

	// 后台任务：创建才计费，列表/取消/重跑中的「重跑」另算一次创建成本
	if strings.HasPrefix(p, "api/v1/tasks") || strings.HasPrefix(p, "agent/tasks") {
		if method == http.MethodPost {
			// POST /tasks        → 新建任务
			// POST /tasks/{id}/retry → 重跑，同样消耗
			if strings.HasSuffix(p, "/retry") || strings.HasSuffix(p, "/tasks") {
				return service.MetricTask, true
			}
		}
		return "", false
	}

	return "", false
}

func metricLabel(metric string) string {
	switch metric {
	case service.MetricChat:
		return "对话"
	case service.MetricSearch:
		return "联网检索"
	case service.MetricTask:
		return "后台任务"
	}
	return "使用"
}
