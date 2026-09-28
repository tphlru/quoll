# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 10, 29.09.2026. Архитектурная сходимость промышленного уровня.
Устранены P0/P1 уязвимости Редакции 9:
- [P0/P1] Уязвимость кросс-цитирования закрыта на уровне БД через композитный внешний ключ `(reply_to_comment_id, interaction_id) REFERENCES comments(id, interaction_id)`.
- [P0/P1] Data Loss бомба (TOCTOU Re-attachment) и потеря истории связей устранены: `comment_attachments` использует Soft-Delete, `UNIQUE(attachment_id)` запрещает повторную привязку.
- [P1] Утечка S3 при OOM воркера и потеря истории очереди устранены: воркер больше не удаляет строки из БД, а делает `status = PURGED` и `processed_at = NOW()`.
- [P1] IDOR привязки чужих вложений закрыт: обязательная проверка `uploaded_by == current_user.id`.
- [P1] Потеря ПДн при Scrub закрыта: Scrub 152-ФЗ каскадно анонимизирует таблицу `comment_versions`.
- [P1] Потеря улик откреплений закрыта: в `comment_versions` добавлено `previous_attachment_ids`.
- [P2] TOCTOU ответа на удаленный коммент: родительский коммент блокируется `FOR SHARE` в иерархии блокировок.
- [P2] Full Table Scans устранены (добавлены индексы для всех FK).

---

## 0. Принятые решения и архитектурные развилки

| # | Вопрос | Решение | Обоснование |
|---|---|---|---|
| **Р1** | Черновики комментариев | Черновиков нет. Создаётся и сразу публикуется | Простота жизненного цикла |
| **Р2** | Привязка к веткам | На 1-4 шагах `branch_id IS NULL`, на 5-8 строго к ветке продукта | Параллельное независимое внедрение |
| **Р3** | Структура обсуждения | Плоская лента с `reply_to_comment_id` | Сохраняет контекст обсуждения |
| **Р4** | Улики (Text & Files) | Редактирует только автор. Старый текст и старые массивы вложений хранятся в `comment_versions`. Привязки не удаляются физически | Полная защита улик от Stealth Edits и Stealth Detach |
| **Р5** | Уведомления | Дедупликация пушей (5 сек окно) | Исключение дублей и спама |
| **Р6** | Безопасность вложений | API проверяет `uploaded_by == current_user.id`. На уровне БД — строгий `UNIQUE(attachment_id)` | Исключение IDOR и кросс-доменных TOCTOU |
| **Р7** | Пройденные и закрытые | Запрет BusinessMutation на закрытых заявках. Разрешены SystemMutation (Scrub) | Защита бизнес-данных |
| **Р8** | Scrub (152-ФЗ) | Анонимизирует `comments.text` и `comment_versions.previous_text` | Полное уничтожение ПДн по закону |
| **Р9** | Retention Period | Открепленные файлы хранятся 30 дней в Outbox, затем очищаются из S3, статус в БД меняется на `PURGED` | Защита расследований |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и матрица прав доступа
- **Менеджер (КАМ) / Руководитель**: Создание, редактирование, soft-delete.
- **Администратор (Admin)**: Модерация, Scrub (152-ФЗ), скачивание любых файлов (через Admin API) в течение 30-дневного Retention Period.

## 1.2 Контекст шагов эталонного воркфлоу
- **Зона А (Шаги 1–4, 3.1):** `branch_id IS NULL`, `side_pointer_id IS NULL`.
- **Шаг 4.1 (Допсоглашение):** Обязателен `side_pointer_id`, `branch_id IS NULL`.
- **Зона Б (Шаги 5–8):** Обязателен `branch_id`, `side_pointer_id IS NULL`.

## 1.3 Жизненный цикл вложений и Transactional Outbox
1. **Загрузка:** Файл в S3. Запись в `attachments` (`uploaded_by = actor`, `status = UPLOADED`).
2. **Привязка:** Проверка `uploaded_by`. `INSERT INTO comment_attachments` + `UPDATE attachments SET status=CLAIMED`. Дубликат ловится через `UniqueViolation`.
3. **Открепление / Soft Delete:** `comment_attachments.is_deleted = true`. Создается запись в `attachment_purge_queue` с `execute_after = now() + 30 days`.
4. **Очистка (Worker):** Через 30 дней: удаляет из S3 -> `UPDATE attachments SET status = PURGED` -> `UPDATE attachment_purge_queue SET processed_at = NOW()`. Нет каскадного удаления, нет падений.

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
    author_id VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
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
    CONSTRAINT chk_comments_author_role CHECK (author_role IN ('manager', 'supervisor', 'admin')),
    CONSTRAINT chk_comments_zones CHECK (branch_id IS NULL OR side_pointer_id IS NULL),
    
    -- Изоляция доменов: композитный ключ для гарантии цитирования только внутри заявки
    CONSTRAINT uq_comments_id_interaction UNIQUE (id, interaction_id),
    CONSTRAINT fk_comments_reply_to FOREIGN KEY (reply_to_comment_id, interaction_id) 
        REFERENCES comments (id, interaction_id) ON DELETE RESTRICT
);

CREATE INDEX ix_comments_interaction_id ON comments(interaction_id);
CREATE INDEX ix_comments_stage_id ON comments(stage_id);
CREATE INDEX ix_comments_branch_id ON comments(branch_id);
CREATE INDEX ix_comments_side_pointer_id ON comments(side_pointer_id);
CREATE INDEX ix_comments_author_id ON comments(author_id);
CREATE INDEX ix_comments_deleted_by ON comments(deleted_by);
CREATE INDEX ix_comments_reply_to ON comments(reply_to_comment_id);

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
    status VARCHAR(50) NOT NULL DEFAULT 'UPLOADED', -- UPLOADED, CLAIMED, PURGED
    uploaded_by VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_attachments_uploaded_by ON attachments(uploaded_by);

CREATE TABLE comment_attachments (
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE RESTRICT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    detached_at TIMESTAMPTZ NULL,
    PRIMARY KEY (comment_id, attachment_id),
    CONSTRAINT uq_comment_attachments_attachment_id UNIQUE (attachment_id)
);

CREATE TABLE attachment_purge_queue (
    id BIGSERIAL PRIMARY KEY,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE RESTRICT,
    storage_key VARCHAR(255) NOT NULL,
    execute_after TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL
);
CREATE INDEX ix_purge_queue_execute_after ON attachment_purge_queue (execute_after) WHERE processed_at IS NULL;
```

## 2.2 Инварианты системы
| # | Инвариант | Уровень |
|---|---|---|
| **C9** | `interaction.status IN (CLOSED, CANCELLED, REJECTED) ⇒ BusinessMutation_forbidden` | Service |
| **C21**| `reply_to_comment_id = R ⇒ R.is_deleted = FALSE` | Service |
| **C22**| `attachment_ids = [A...] ⇒ ∀A A.uploaded_by == current_user.id` | Service |

## 2.3 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`.
2. `Lock Parents`: 
   `SELECT * FROM interactions FOR SHARE`
   `SELECT * FROM branches FOR SHARE`
   `SELECT * FROM side_pointers FOR SHARE`
   `SELECT * FROM comments WHERE id = :reply_to_comment_id FOR SHARE` -- TOCTOU защита ответа на удаленный
3. `Lock Child`: `SELECT * FROM comments FOR UPDATE`.

## 2.4 DTO
Использование `set[int]` для вложений.
