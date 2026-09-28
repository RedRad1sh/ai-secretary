-- 0001_indexes.sql — индексы под «горячие» запросы бота.
--
-- Первая миграция заодно служит примером: идемпотентные конструкции
-- (IF NOT EXISTS) обязательны — файл может быть применён на любую базу,
-- включая свежую и «старую продакшн».
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders (sent, remind_at);

CREATE INDEX IF NOT EXISTS idx_events_start
    ON events (start_iso);

CREATE INDEX IF NOT EXISTS idx_events_alive
    ON events (deleted, id DESC);

CREATE INDEX IF NOT EXISTS idx_reqlog_created
    ON reqlog (created_at);
