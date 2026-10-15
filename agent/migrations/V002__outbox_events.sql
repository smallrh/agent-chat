-- ============================================================================
-- Outbox Pattern: 保证 Chat API 写入和 Kafka 发送的最终一致性
-- 
-- 状态机（status）:
--   0 = PENDING           — 待 relay 发 Kafka
--   1 = SENT              — 已发 Kafka，sent_at 有值
--   2 = FAILED_EXHAUSTED  — 重试耗尽，已直写 PG 兜底（终态）
--
-- 清理策略: sent_at / created_at 超过 7 天的 SENT / FAILED_EXHAUSTED 记录
-- ============================================================================

CREATE TABLE IF NOT EXISTS outbox_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        UUID NOT NULL UNIQUE,                 -- 幂等键，对应 MemoryEvent.event_id
    topic           VARCHAR(128) NOT NULL,
    key             VARCHAR(512),                          -- Kafka partition key (conversation_id)
    payload         JSONB NOT NULL,                        -- MemoryEvent 完整 JSON
    status          SMALLINT NOT NULL DEFAULT 0,           -- 见上方状态机
    retry_count     INT NOT NULL DEFAULT 0,
    error           TEXT,                                  -- 最近一次失败原因
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

-- relay 高频扫 pending：只索引 status=0 的行（部分索引），降低写入成本
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events(id)
    WHERE status = 0;

-- 清理任务扫终态：按 sent_at / created_at 范围删
CREATE INDEX IF NOT EXISTS idx_outbox_terminal
    ON outbox_events(status, sent_at, created_at)
    WHERE status IN (1, 2);
