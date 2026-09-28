# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 18 (Flawless Monolith), 29.09.2026.
Устранены последние P2, найденные Техническим Аудитором в 16-й редакции (опечатки CHECK-констрейнтов, Dead Letter Queue и защита от переноса родителей в другие Зоны).

---

## 0. Архитектурные решения
| # | Вопрос | Решение |
|---|---|---|
| **Р1** | Изоляция Зон | Гарантируется триггером в СУБД (C22). Запрещен перенос существующих комментариев в другие зоны. |
| **Р2** | Scrub (152-ФЗ) | Анонимизирует ПДн и глобально обнуляет все FK. Разделяет логику авторов и модераторов. |
| **Р3** | Lock-Free S3 | Воркеры очереди не держат транзакции БД во время I/O. Зависшие задачи восстанавливаются. Необрабатываемые файлы падают в Dead Letter Queue (`FAILED`). |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и инварианты
- **C21**: `reply_to_comment_id = R ⇒ R.is_deleted = FALSE`. **Запрет каскадов**: API отклоняет Soft-Delete комментария, если у него есть активные ответы.
- **C22**: Изоляция Зон. (Обеспечивается триггером БД).
- **C23**: `is_scrubbed = TRUE ∨ is_deleted = TRUE ⇒ PATCH_forbidden`.
- **C24**: Авторизация. `Edit / Delete ⇒ author_id == current_user.id ∨ role ∈ (supervisor, admin)`.

## 1.2 Жизненный цикл вложений и данных (Zero Leaks)
1. **Загрузка:** `INSERT INTO attachments` (`status = UPLOADED`).
2. **Привязка:** Атомарный `UPDATE attachments SET status='CLAIMED', updated_at=NOW() WHERE id = :id AND status='UPLOADED' RETURNING id`. Вставка в `comment_attachments`.
3. **Orphan Sweep:** Фоновый Job 1h: `UPDATE attachments SET status='PURGING', updated_at=NOW() WHERE id IN (SELECT id FROM attachments WHERE (status='UPLOADED' AND created_at < NOW() - 24h) OR (status='PURGING' AND updated_at < NOW() - 1h) FOR UPDATE SKIP LOCKED)`. Удаляет из S3 -> `UPDATE status='PURGED'`.
4. **Soft-Delete Коммента / Detach через Edit:** Для коммента: `is_deleted = true`, `deleted_at = NOW()`. Для файла: `detached_at = NOW()` в `comment_attachments`. `UPSERT` файла в `attachment_purge_queue` (`reason='SOFT_DELETE'`, `status='PENDING'`). Если Заявка `CLOSED` -> `execute_after = NOW() + 30 days`, иначе `NULL`.
5. **Scrub (152-ФЗ):** Разрешает удаление пользователя из БД.
   - Глобальное обнуление метаданных: `UPDATE attachments SET uploaded_by = NULL WHERE uploaded_by = :id` и `UPDATE comments SET deleted_by = NULL WHERE deleted_by = :id`.
   - Выжигает ПДн: `UPDATE comments SET text='[УДАЛЕНО]', author_name='[УДАЛЕНО]', author_id=NULL, is_scrubbed=true WHERE author_id = :id`.
   - Выжигает историю: `UPDATE comment_versions SET previous_text='[УДАЛЕНО]' WHERE comment_id IN (SELECT id FROM comments WHERE author_id = :id)`.
   - Выжигает файлы: `UPSERT` всех файлов коммента в очередь с `reason='SCRUB', execute_after=NOW(), status='PENDING'`. 
6. **Закрытие Заявки:** `UPDATE attachment_purge_queue SET execute_after = NOW() + 30 days, updated_at=NOW() WHERE interaction_id = :id AND execute_after IS NULL AND status NOT IN ('DONE', 'FAILED')`.
7. **Reopen Заявки:** `UPDATE attachment_purge_queue SET execute_after = NULL, status = 'PENDING', updated_at=NOW() WHERE interaction_id = :id AND reason='SOFT_DELETE' AND status NOT IN ('DONE', 'FAILED')`.
8. **Очистка улик (Lock-Free Worker & Crash Recovery):** 
   - *Фаза 1 (DB)*: `UPDATE attachment_purge_queue SET status='PROCESSING', updated_at=NOW() WHERE attachment_id IN (SELECT attachment_id FROM attachment_purge_queue WHERE (status='PENDING' AND execute_after <= NOW()) OR (status='PROCESSING' AND updated_at < NOW() - 1h) FOR UPDATE SKIP LOCKED) RETURNING storage_key`.
   - *Фаза 2 (Network)*: Вызов S3 DELETE (Транзакция БД отпущена).
   - *Фаза 3 (DB)*:
     - *Успех*: `UPDATE queue SET status='DONE', processed_at = NOW()`. `UPDATE attachments SET status='PURGED'`.
     - *Ошибка (S3)*: `UPDATE queue SET status = CASE WHEN attempts >= 3 THEN 'FAILED' ELSE 'PENDING' END, attempts = attempts + 1, updated_at=NOW(), execute_after = CASE WHEN execute_after IS NOT NULL THEN NOW() + INTERVAL '1h' ELSE NULL END`.
9. **Queue Retention:** Фоновый Job удаляет закрытые задачи из очереди: `DELETE FROM attachment_purge_queue WHERE status = 'DONE' AND processed_at < NOW() - 7 days`.

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
    CONSTRAINT chk_comments_zones CHECK (branch_id IS NULL OR side_pointer_id IS NULL),
    CONSTRAINT chk_comments_deleted CHECK (is_deleted = FALSE OR deleted_at IS NOT NULL),
    
    CONSTRAINT uq_comments_id_interaction UNIQUE (id, interaction_id),
    CONSTRAINT fk_comments_reply_to FOREIGN KEY (reply_to_comment_id, interaction_id) 
        REFERENCES comments (id, interaction_id) ON DELETE RESTRICT
);

CREATE INDEX ix_comments_interaction_id ON comments(interaction_id);
CREATE INDEX ix_comments_reply_to ON comments(reply_to_comment_id);
CREATE INDEX ix_comments_author_id ON comments(author_id);

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
    uploaded_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_attachments_status CHECK (status IN ('UPLOADED', 'CLAIMED', 'PURGING', 'PURGED'))
);
CREATE INDEX ix_attachments_uploaded_by ON attachments(uploaded_by);
CREATE INDEX ix_attachments_orphan_sweep ON attachments (status, created_at) WHERE status IN ('UPLOADED', 'PURGING');

CREATE TABLE comment_attachments (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE RESTRICT,
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    detached_at TIMESTAMPTZ NULL
);
CREATE INDEX ix_comment_attachments_comment_id ON comment_attachments(comment_id);

CREATE TABLE attachment_purge_queue (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE RESTRICT,
    interaction_id INTEGER NOT NULL REFERENCES interactions(id) ON DELETE RESTRICT,
    storage_key VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    reason VARCHAR(50) NOT NULL,
    execute_after TIMESTAMPTZ NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL,

    CONSTRAINT chk_queue_status CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED'))
);
CREATE INDEX ix_purge_queue_execute_after ON attachment_purge_queue (execute_after) WHERE status = 'PENDING';
CREATE INDEX ix_purge_queue_interaction ON attachment_purge_queue (interaction_id);
CREATE INDEX ix_purge_queue_processing ON attachment_purge_queue (updated_at) WHERE status = 'PROCESSING';
CREATE INDEX ix_purge_queue_done ON attachment_purge_queue (processed_at) WHERE status = 'DONE';

-- Триггер для аппаратной гарантии изоляции зон (C22)
CREATE OR REPLACE FUNCTION check_zones() RETURNS TRIGGER AS $$
DECLARE
    p_branch BIGINT;
    p_side BIGINT;
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.branch_id IS DISTINCT FROM OLD.branch_id OR NEW.side_pointer_id IS DISTINCT FROM OLD.side_pointer_id) THEN
        RAISE EXCEPTION 'Cannot change zone of existing comment';
    END IF;

    IF NEW.reply_to_comment_id IS NOT NULL THEN
        SELECT branch_id, side_pointer_id INTO p_branch, p_side FROM comments WHERE id = NEW.reply_to_comment_id;
        IF p_branch IS DISTINCT FROM NEW.branch_id OR p_side IS DISTINCT FROM NEW.side_pointer_id THEN
            RAISE EXCEPTION 'Zone isolation violated (C22)';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_zones BEFORE INSERT OR UPDATE ON comments
FOR EACH ROW EXECUTE FUNCTION check_zones();
```

## 2.2 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`
2. `Lock Parents`: `FOR SHARE` (interaction, branch, side_pointer, parent_comment `is_deleted = false`)
3. `Lock Child`: `FOR UPDATE` (`is_scrubbed = false AND is_deleted = false`)
