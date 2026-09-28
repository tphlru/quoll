# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 14, 29.09.2026. Архитектурная сходимость промышленного уровня (Release Candidate).
Устранены последние выявленные краевые P1/P2 уязвимости:
- [P1] Scrub (152-ФЗ) теперь выжигает ПДн и в истории: каскадный `UPDATE comment_versions SET previous_text = '[УДАЛЕНО]'`.
- [P1] Закрыт S3 Leak при откреплении файла через Edit (теперь он корректно уходит в очередь очистки наравне с Soft-Delete).
- [P1] Закрыт срыв кулдауна в Orphan Sweep (теперь обновляется `updated_at=NOW()`).
- [P2] Очередь очистки `attachment_purge_queue` получила `interaction_id` для безопасного O(1) пересчета таймеров при закрытии/переоткрытии заявки без джоинов.
- [P2] Идемпотентность UPSERT: статус `SCRUB` в очереди стал приоритетным и не может быть понижен до `SOFT_DELETE`.
- [P2] Защита от Poison Pill: сбои S3 в воркере инкрементируют `attempts` и делают backoff в отдельной транзакции.
- [P2] Добавлены индексы для защиты от Seq Scan Table Lock при `ON DELETE SET NULL` на профилях пользователей.

---

## 0. Архитектурные решения
| # | Вопрос | Решение |
|---|---|---|
| **Р1** | Изоляция Зон | Строгая проверка совпадения `branch_id` И `side_pointer_id` при ответах (C22) |
| **Р2** | Scrub (152-ФЗ) | Анонимизирует `text`, `previous_text`, `author_name`. При удалении юзера каскадно срабатывает `SET NULL` на FK, плюс сервис подхватывает событие для Scrub'а его комментов |
| **Р3** | Безопасность улик | Файлы удаленных комментов и открепленные через Edit ждут закрытия Заявки + 30 дней. Scrub-файлы уничтожаются мгновенно |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и инварианты
- **C21**: `reply_to_comment_id = R ⇒ R.is_deleted = FALSE` (Прекондишен при создании).
- **C22**: Изоляция Зон. `parent.branch_id IS NOT DISTINCT FROM child.branch_id AND parent.side_pointer_id IS NOT DISTINCT FROM child.side_pointer_id`.
- **C23**: `is_scrubbed = TRUE ∨ is_deleted = TRUE ⇒ PATCH_forbidden`.
- **C24**: Авторизация. `Edit / Delete ⇒ author_id == current_user.id ∨ role ∈ (supervisor, admin)`.

## 1.2 Жизненный цикл вложений (Zero S3 Leaks)
1. **Загрузка:** `INSERT INTO attachments` (`status = UPLOADED`).
2. **Привязка:** Атомарный `UPDATE attachments SET status='CLAIMED', updated_at=NOW() WHERE id = :id AND status='UPLOADED' RETURNING id`. Вставка в `comment_attachments`.
3. **Orphan Sweep (Мусор):** Фоновый Job каждый 1h: `UPDATE attachments SET status='PURGING', updated_at=NOW() WHERE id IN (SELECT id FROM attachments WHERE (status='UPLOADED' AND created_at < NOW() - 24h) OR (status='PURGING' AND updated_at < NOW() - 1h) FOR UPDATE SKIP LOCKED)`. Удаляет из S3 -> `UPDATE status='PURGED'`.
4. **Soft-Delete Коммента / Detach через Edit:** `is_deleted = true`, `detached_at = NOW()`. Вставка файла в `attachment_purge_queue` (`reason='SOFT_DELETE'`). Если Заявка `CLOSED` -> `execute_after = NOW() + 30 days`, иначе `NULL`. Идемпотентный UPSERT не понижает приоритет `SCRUB`.
5. **Scrub Комментария:** Выжигает ПДн: `text = '[УДАЛЕНО]', author_name = '[УДАЛЕНО]'`. Каскадно выжигает историю: `UPDATE comment_versions SET previous_text = '[УДАЛЕНО]' WHERE comment_id = :id`. Вставка файлов в очередь с `reason='SCRUB', execute_after=NOW()`.
6. **Закрытие Заявки:** `UPDATE attachment_purge_queue SET execute_after = NOW() + 30 days WHERE interaction_id = :id AND execute_after IS NULL AND processed_at IS NULL`.
7. **Reopen Заявки:** `UPDATE attachment_purge_queue SET execute_after = NULL WHERE interaction_id = :id AND reason='SOFT_DELETE' AND processed_at IS NULL`.
8. **Очистка улик (Worker):** Читает очередь `WHERE execute_after <= NOW() FOR UPDATE SKIP LOCKED`. При успехе: удаляет из S3 -> `UPDATE attachments SET status='PURGED'` -> `UPDATE queue SET processed_at = NOW()`. При сетевой ошибке S3: в отдельной транзакции (чтобы не откатилось) `UPDATE queue SET attempts = attempts + 1, execute_after = NOW() + 1h`.

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
    author_id VARCHAR(255) NULL REFERENCES users(id) ON DELETE SET NULL,
    author_name VARCHAR(255) NOT NULL,
    author_role VARCHAR(50) NOT NULL,
    reply_to_comment_id BIGINT NULL,
    text TEXT NOT NULL,
    is_edited BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    is_scrubbed BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ NULL,
    deleted_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_comments_text_not_empty CHECK (text ~ '[^\s]' OR is_scrubbed = TRUE),
    CONSTRAINT chk_comments_zones CHECK (branch_id IS NULL OR side_pointer_id IS NULL),
    CONSTRAINT chk_comments_deleted CHECK (is_deleted = FALSE OR deleted_at IS NOT NULL),
    
    CONSTRAINT uq_comments_id_interaction UNIQUE (id, interaction_id),
    CONSTRAINT fk_comments_reply_to FOREIGN KEY (reply_to_comment_id, interaction_id) 
        REFERENCES comments (id, interaction_id) ON DELETE RESTRICT
);

-- Индексы для FK и предотвращения Table Lock при ON DELETE SET NULL
CREATE INDEX ix_comments_interaction_id ON comments(interaction_id);
CREATE INDEX ix_comments_reply_to ON comments(reply_to_comment_id);
CREATE INDEX ix_comments_author_id ON comments(author_id);
CREATE INDEX ix_comments_deleted_by ON comments(deleted_by);

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
    uploaded_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
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
    interaction_id INTEGER NOT NULL REFERENCES interactions(id) ON DELETE RESTRICT,
    storage_key VARCHAR(255) NOT NULL,
    reason VARCHAR(50) NOT NULL, -- 'SCRUB', 'SOFT_DELETE'
    execute_after TIMESTAMPTZ NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL
);
CREATE INDEX ix_purge_queue_execute_after ON attachment_purge_queue (execute_after) WHERE processed_at IS NULL;
CREATE INDEX ix_purge_queue_interaction ON attachment_purge_queue (interaction_id);
```

## 2.2 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`
2. `Lock Parents`: `FOR SHARE` (interaction, branch, side_pointer, parent_comment `is_deleted = false`)
3. `Lock Child`: `FOR UPDATE` (`is_scrubbed = false AND is_deleted = false`)
