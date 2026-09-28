# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 15 (Final Monolith), 29.09.2026. 
Закрыты фундаментальные уязвимости баз данных и распределенных транзакций:
- [P1] Отстрел 152-ФЗ устранен: `ON DELETE SET NULL` возвращен на `RESTRICT`. Оркестратор обязан выполнить Scrub ДО удаления пользователя (гарантия анонимизации).
- [P1] Deadlock API-запросов и TOCTOU при S3-сбоях: Воркер очереди теперь lock-free. Используется транзитный `status='PROCESSING'`, блокировка БД отпускается до сетевого вызова S3. Backoff-защита не затирает "спасенные" переоткрытием заявки файлы.
- [P1] Бесконечный цикл и Seq Scan воркера устранены: Добавлены предикаты `processed_at IS NULL` и частичные индексы на `attachments` для Orphan Sweep.
- [P2] Выжжена история версий при Scrub: `previous_text` очищается каскадно.

---

## 0. Архитектурные решения
| # | Вопрос | Решение |
|---|---|---|
| **Р1** | Изоляция Зон | Строгая проверка `branch_id` И `side_pointer_id` (C22) |
| **Р2** | Scrub (152-ФЗ) | Анонимизирует `text`, `previous_text`, `author_name`. **Синхронно блокирует** удаление пользователя (`ON DELETE RESTRICT`) до успешного выжигания ПДн оркестратором. |
| **Р3** | Lock-Free S3 | Фоновые воркеры не держат транзакции БД во время сетевых I/O (S3), предотвращая деградацию API. |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и инварианты
- **C21**: `reply_to_comment_id = R ⇒ R.is_deleted = FALSE`.
- **C22**: Изоляция Зон. `parent.branch_id IS NOT DISTINCT FROM child.branch_id AND parent.side_pointer_id IS NOT DISTINCT FROM child.side_pointer_id`.
- **C23**: `is_scrubbed = TRUE ∨ is_deleted = TRUE ⇒ PATCH_forbidden`.
- **C24**: `Edit / Delete ⇒ author_id == current_user.id ∨ role ∈ (supervisor, admin)`.

## 1.2 Жизненный цикл вложений (Zero S3 Leaks)
1. **Загрузка:** `INSERT INTO attachments` (`status = UPLOADED`).
2. **Привязка:** Атомарный `UPDATE attachments SET status='CLAIMED', updated_at=NOW() WHERE id = :id AND status='UPLOADED' RETURNING id`. Вставка в `comment_attachments`.
3. **Orphan Sweep (Мусор):** Фоновый Job 1h: `UPDATE attachments SET status='PURGING', updated_at=NOW() WHERE id IN (SELECT id FROM attachments WHERE (status='UPLOADED' AND created_at < NOW() - 24h) OR (status='PURGING' AND updated_at < NOW() - 1h) FOR UPDATE SKIP LOCKED)`. Удаляет из S3 -> `UPDATE status='PURGED'`.
4. **Soft-Delete Коммента / Detach через Edit:** `is_deleted = true`, `detached_at = NOW()`. `UPSERT` файла в `attachment_purge_queue` (`reason='SOFT_DELETE'`, `status='PENDING'`). Если Заявка `CLOSED` -> `execute_after = NOW() + 30 days`, иначе `NULL`.
5. **Scrub Комментария (152-ФЗ):** `text = '[УДАЛЕНО]'`, `author_name = '[УДАЛЕНО]'`. Каскадно выжигает историю: `UPDATE comment_versions SET previous_text = '[УДАЛЕНО]' WHERE comment_id = :id`. Мгновенная очередь: `UPSERT` с `reason='SCRUB', execute_after=NOW(), status='PENDING'`. При конфликте статус `SCRUB` приоритетнее `SOFT_DELETE`.
6. **Закрытие Заявки:** `UPDATE attachment_purge_queue SET execute_after = NOW() + 30 days WHERE interaction_id = :id AND execute_after IS NULL AND status != 'DONE'`.
7. **Reopen Заявки:** `UPDATE attachment_purge_queue SET execute_after = NULL WHERE interaction_id = :id AND reason='SOFT_DELETE' AND status != 'DONE'`.
8. **Очистка улик (Lock-Free Worker):** 
   - *Фаза 1 (DB)*: `UPDATE attachment_purge_queue SET status='PROCESSING' WHERE attachment_id IN (SELECT attachment_id FROM attachment_purge_queue WHERE status='PENDING' AND execute_after <= NOW() FOR UPDATE SKIP LOCKED) RETURNING storage_key`.
   - *Фаза 2 (Network)*: Воркер вызывает S3 DELETE. Транзакция БД свободна.
   - *Фаза 3 (DB)*: 
     - Успех S3: `UPDATE attachment_purge_queue SET status='DONE', processed_at = NOW() WHERE attachment_id = X`.
     - Ошибка S3 (Poison Pill): `UPDATE attachment_purge_queue SET status='PENDING', attempts = attempts + 1, execute_after = NOW() + 1h WHERE attachment_id = X AND execute_after IS NOT NULL` (спасенные файлы игнорируются).

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
    processed_at TIMESTAMPTZ NULL,

    CONSTRAINT chk_queue_status CHECK (status IN ('PENDING', 'PROCESSING', 'DONE'))
);
CREATE INDEX ix_purge_queue_execute_after ON attachment_purge_queue (execute_after) WHERE status = 'PENDING';
CREATE INDEX ix_purge_queue_interaction ON attachment_purge_queue (interaction_id);
```

## 2.2 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`
2. `Lock Parents`: `FOR SHARE` (interaction, branch, side_pointer, parent_comment `is_deleted = false`)
3. `Lock Child`: `FOR UPDATE` (`is_scrubbed = false AND is_deleted = false`)
