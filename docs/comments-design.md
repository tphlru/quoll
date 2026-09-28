# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 13, 29.09.2026. Архитектурная сходимость промышленного уровня.
Устранены P1/P2 Редакции 12:
- [P1] 152-ФЗ блокировался констрейнтом: `uploaded_by` и `deleted_by` теперь `ON DELETE SET NULL`.
- [P1] Конфликт ключей (Scrub vs Soft-Delete): вставка в очередь использует `UPSERT (ON CONFLICT DO UPDATE)`.
- [P1] TOCTOU Orphan Sweep vs Привязка: привязка работает через атомарный `UPDATE ... WHERE status='UPLOADED' RETURNING id`.
- [P1] Утечка S3 при Soft-Delete в уже закрытой заявке: таймер ставится сразу на `NOW() + 30 days`, если заявка закрыта.
- [P2] Потеря улик при Reopen: при переоткрытии заявки таймер в очереди сбрасывается в `NULL`.
- [P2] Зомби-файлы: Orphan Sweep подбирает `PURGING` записи старше 1 часа.
- [P2] Каскадное удаление: Soft-Delete коммента явно каскадирует удаление вложений.
- [P2] Авторизация и Зоны: прописаны строгие инварианты на мутации (только автор) и изоляцию веток.

---

## 0. Архитектурные решения
| # | Вопрос | Решение |
|---|---|---|
| **Р1** | Изоляция Зон | На 1-4 шагах `branch_id IS NULL`. Строгая проверка совпадения `branch_id` при ответах |
| **Р2** | Scrub (152-ФЗ) | Полная анонимизация текста. Удаленные юзеры обнуляются каскадно (`ON DELETE SET NULL`) |
| **Р3** | Улики расследований | Файлы от удаленных комментов ждут закрытия Заявки + 30 дней. При переоткрытии (Reopen) таймер сбрасывается |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и мутации
- Инвариант авторизации: Мутации (Edit / Soft-Delete) разрешены только если `author_id == current_user.id` ИЛИ `role IN ('supervisor', 'admin')`.

## 1.2 Жизненный цикл вложений (Zero S3 Leaks)
1. **Загрузка:** `INSERT INTO attachments` (`status = UPLOADED`).
2. **Привязка:** Атомарный `UPDATE attachments SET status='CLAIMED' WHERE id = :id AND status='UPLOADED' RETURNING id`. (Защита от Orphan Sweep). Вставка в `comment_attachments`.
3. **Orphan Sweep (Сборка мусора):** Фоновый Job каждые 1h находит `status = 'UPLOADED' AND created_at < NOW() - 24h` ИЛИ `status = 'PURGING' AND updated_at < NOW() - 1h`. Переводит в `PURGING`, удаляет из S3, ставит `PURGED`.
4. **Soft Delete Комментария:** Устанавливает `is_deleted = true`. **Каскадно** ставит `is_deleted = true` для всех его файлов в `comment_attachments`. Делает `UPSERT` файлов в `attachment_purge_queue` (`reason='SOFT_DELETE'`). Если Заявка `CLOSED` — `execute_after = NOW() + 30 days`, иначе `NULL`.
5. **Scrub Комментария:** Делает `UPSERT` файлов в очередь с `reason='SCRUB', execute_after=NOW()`.
6. **Закрытие Заявки:** `UPDATE attachment_purge_queue SET execute_after = NOW() + 30 days WHERE execute_after IS NULL AND ...`.
7. **Reopen Заявки:** `UPDATE attachment_purge_queue SET execute_after = NULL WHERE reason='SOFT_DELETE' AND ...`.
8. **Очистка улик (Worker):** Читает очередь (`WHERE execute_after <= NOW() FOR UPDATE SKIP LOCKED`). Удаляет из S3 -> `UPDATE attachments SET status='PURGED'` -> `UPDATE attachment_purge_queue SET processed_at = NOW()`.

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

CREATE INDEX ix_comments_interaction_id ON comments(interaction_id);
CREATE INDEX ix_comments_reply_to ON comments(reply_to_comment_id);

CREATE TABLE comment_versions (
    id BIGSERIAL PRIMARY KEY,
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    previous_text TEXT NOT NULL,
    previous_attachment_ids INTEGER[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE attachments (
    id SERIAL PRIMARY KEY,
    storage_key VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'UPLOADED',
    uploaded_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_attachments_status CHECK (status IN ('UPLOADED', 'CLAIMED', 'PURGING', 'PURGED'))
);

CREATE TABLE comment_attachments (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE RESTRICT,
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    detached_at TIMESTAMPTZ NULL
);

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

## 2.2 Инварианты системы (Service Layer)
| # | Инвариант |
|---|---|
| **C21**| `reply_to_comment_id = R ⇒ R.is_deleted = FALSE` (Прекондишен создания) |
| **C22**| `reply_to_comment_id = R ⇒ parent.branch_id == child.branch_id` (Изоляция веток) |
| **C23**| `is_scrubbed = TRUE ∨ is_deleted = TRUE ⇒ PATCH_forbidden` |
| **C24**| `Edit/Delete ⇒ author_id == current_user.id ∨ role ∈ (supervisor, admin)` |

## 2.3 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`.
2. `Lock Parents`: `FOR SHARE` (interaction, branch, parent_comment `is_deleted = false`).
3. `Lock Child`: `FOR UPDATE` (`is_scrubbed = false AND is_deleted = false`).
