# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 12, 29.09.2026. Архитектурная сходимость промышленного уровня.
Устранены все P0/P1 уязвимости Редакции 11:
- [P1] Закрыт Hot Loop воркера и задержка Scrub: в `attachment_purge_queue` возвращен `execute_after` и введено поле `reason` (Scrub/Soft-Delete). Scrub удаляет файлы мгновенно, Soft-Delete ждет закрытия Заявки.
- [P1] Закрыта утечка 152-ФЗ по `author_id`: поле `author_id` теперь `NULLABLE`. При Scrub сбрасывается в `NULL`, полностью уничтожая ПДн автора.
- [P1] Закрыт TOCTOU при Orphan Sweep: введен транзитный статус `PURGING`.
- [P2] Закрыт TOCTOU (Edit vs Soft-Delete): редактирование требует лок `is_deleted = false AND is_scrubbed = false FOR UPDATE`.
- [P3] Устранены дубли в очереди: `attachment_id` стал `PRIMARY KEY` в таблице очереди очистки.

---

## 0. Принятые решения и архитектурные развилки

| # | Вопрос | Решение | Обоснование |
|---|---|---|---|
| **Р1** | Привязка к веткам | На 1-4 шагах `branch_id IS NULL`, на 5-8 строго к ветке продукта | Изоляция Зон |
| **Р2** | Улики (Text & Files) | Старый текст и вложения хранятся в `comment_versions`. Привязки иммутабельны | Защита от Stealth Edits |
| **Р3** | Безопасность вложений | Строгий `PRIMARY KEY(attachment_id)` в `comment_attachments` | Нет IDOR, нет TOCTOU |
| **Р4** | Scrub (152-ФЗ) | Анонимизирует `text`, обнуляет `author_id`, ставит `author_name='[УДАЛЕНО]'`, очищает `previous_text`. Все файлы летят в Outbox с `reason='SCRUB'` и мгновенно уничтожаются | Полное соответствие закону |
| **Р5** | Улики расследований | Открепленные через бизнес-логику файлы летят в Outbox с `reason='SOFT_DELETE'` и ждут закрытия Заявки + 30 дней | Защита расследований инцидентов |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и права
- **Менеджер / Руководитель**: Создание, редактирование, soft-delete.
- **Админ**: Scrub (152-ФЗ), расследования (скачивание файлов до их purge).

## 1.2 Жизненный цикл вложений (Zero S3 Leaks)
1. **Загрузка:** `INSERT INTO attachments` (`status = UPLOADED`, `storage_key`).
2. **Orphan Sweep:** Фоновый Job (каждый час): `UPDATE attachments SET status='PURGING' WHERE status='UPLOADED' AND created_at < NOW() - 24h RETURNING id`. Удаляет из S3 -> `UPDATE status='PURGED'`. (Защита от гонок и брошенных загрузок).
3. **Привязка:** Проверка `uploaded_by`. `INSERT INTO comment_attachments` + `UPDATE attachments SET status='CLAIMED'`.
4. **Открепление / Soft Delete:** `comment_attachments.is_deleted = true`. Запись падает в `attachment_purge_queue` (`reason='SOFT_DELETE'`, `execute_after=NULL`).
5. **Закрытие Заявки (Триггер):** Когда Заявка переходит в `CLOSED`, всем её файлам в очереди проставляется `execute_after = NOW() + 30 days`.
6. **Очистка улик (Worker):** Воркер читает очередь (`WHERE execute_after <= NOW() FOR UPDATE SKIP LOCKED`). Удаляет из S3 -> `UPDATE attachments SET status='PURGED'` -> `UPDATE attachment_purge_queue SET processed_at = NOW()`.

---

# Часть II. Технический уровень

## 2.1 Схема БД (DDL)

```sql
CREATE TABLE comments (
    id BIGSERIAL PRIMARY KEY,
    interaction_id INTEGER NOT NULL REFERENCES interactions(id) ON DELETE RESTRICT,
    stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE RESTRICT,
    branch_id BIGINT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    side_pointer_id BIGINT NULL REFERENCES side_pointers(id) ON DELETE RESTRICT,
    author_id VARCHAR(255) NULL REFERENCES users(id) ON DELETE RESTRICT,
    author_name VARCHAR(255) NOT NULL,
    author_role VARCHAR(50) NOT NULL,
    reply_to_comment_id BIGINT NULL,
    text TEXT NOT NULL,
    is_edited BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    is_scrubbed BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ NULL,
    deleted_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_comments_text_not_empty CHECK (text ~ '[^\s]' OR is_scrubbed = TRUE),
    CONSTRAINT chk_comments_author_role CHECK (author_role IN ('manager', 'supervisor', 'admin')),
    CONSTRAINT chk_comments_zones CHECK (branch_id IS NULL OR side_pointer_id IS NULL),
    
    -- Изоляция доменов: кросс-цитирование запрещено
    CONSTRAINT uq_comments_id_interaction UNIQUE (id, interaction_id),
    CONSTRAINT fk_comments_reply_to FOREIGN KEY (reply_to_comment_id, interaction_id) 
        REFERENCES comments (id, interaction_id) ON DELETE RESTRICT
);

CREATE INDEX ix_comments_interaction_id ON comments(interaction_id);
CREATE INDEX ix_comments_reply_to ON comments(reply_to_comment_id);
-- Опущены остальные индексы для краткости (они есть)

CREATE TABLE comment_versions (
    id BIGSERIAL PRIMARY KEY,
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    previous_text TEXT NOT NULL,
    previous_attachment_ids INTEGER[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_comment_versions_comment_id ON comment_versions(comment_id);

CREATE TABLE attachments (
    id SERIAL PRIMARY KEY,
    storage_key VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'UPLOADED',
    uploaded_by VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_attachments_status CHECK (status IN ('UPLOADED', 'CLAIMED', 'PURGING', 'PURGED'))
);
CREATE INDEX ix_attachments_uploaded_by ON attachments(uploaded_by);

CREATE TABLE comment_attachments (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE RESTRICT,
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    detached_at TIMESTAMPTZ NULL
);
CREATE INDEX ix_comment_attachments_comment_id ON comment_attachments(comment_id);

CREATE TABLE attachment_purge_queue (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE RESTRICT,
    storage_key VARCHAR(255) NOT NULL,
    reason VARCHAR(50) NOT NULL, -- 'SCRUB', 'SOFT_DELETE'
    execute_after TIMESTAMPTZ NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL
);
CREATE INDEX ix_purge_queue_execute_after ON attachment_purge_queue (execute_after) WHERE processed_at IS NULL;
```

## 2.2 Инварианты системы
| # | Инвариант | Уровень |
|---|---|---|
| **C21**| `reply_to_comment_id = R ⇒ R.is_deleted = FALSE` (Прекондишен создания) | Service |
| **C22**| `attachment_ids = [A...] ⇒ ∀A A.uploaded_by == current_user.id ∧ A.status = UPLOADED` | Service |
| **C23**| `is_scrubbed = TRUE ∨ is_deleted = TRUE ⇒ PATCH_forbidden` | Service |

## 2.3 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`.
2. `Lock Parents`: 
   `SELECT * FROM interactions FOR SHARE`
   `SELECT * FROM branches FOR SHARE`
   `SELECT * FROM side_pointers FOR SHARE`
   `SELECT * FROM comments WHERE id = :reply_to_comment_id AND is_deleted = false FOR SHARE`
3. `Lock Child`: `SELECT * FROM comments WHERE is_scrubbed = false AND is_deleted = false FOR UPDATE`.
