# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 9, 28.09.2026. Архитектурная сходимость промышленного уровня.
Устранены P0/P1 уязвимости Редакции 8:
- [P0] Deadlock воркера очистки S3 (устранен: воркер предварительно разрывает связь в `comment_attachments` перед удалением файла).
- [P1] Уничтожение текстовых улик (внедрена таблица `comment_versions` для сохранения истории редактирования).
- [P1] Обход Retention Period при откреплении вложений (открепление через PATCH теперь переносит файл в Outbox).
- [P1] Уязвимость кросс-доменных ссылок (внедрен инвариант полного совпадения контекста для цитирования и Composite FK-логика).
- [P2] Полное сканирование таблиц при блокировках (добавлены индексы для всех FK).
- [P2] Коллизия Зон (добавлен `CHECK (branch_id IS NULL OR side_pointer_id IS NULL)`).
- [P2] Цитирование удаленных сообщений (добавлен запрет `R.is_deleted = false`).
- [P3] Упрощение проверки TOCTOU (теперь опираемся на нативный `UniqueViolation`).

---

## 0. Принятые решения и архитектурные развилки

| # | Вопрос | Решение | Обоснование |
|---|---|---|---|
| **Р1** | Черновики комментариев | Черновиков нет. Создаётся и сразу публикуется | Простота жизненного цикла |
| **Р2** | Привязка к веткам | На 1-4 шагах `branch_id IS NULL`, на 5-8 строго к ветке продукта | Параллельное независимое внедрение |
| **Р3** | Структура обсуждения | Плоская лента с `reply_to_comment_id` | Сохраняет контекст обсуждения |
| **Р4** | Редактирование и улики | Редактирует только автор. Старый текст сохраняется в `comment_versions` | Защита от уничтожения улик (stealth edits) |
| **Р5** | Уведомления | Дедупликация пушей (5 сек окно) | Исключение дублей и спама |
| **Р6** | Безопасность вложений | `UNIQUE(attachment_id)`. Перехват `UniqueViolation` (23505) в слое БД | Нативная СУБД защита от TOCTOU |
| **Р7** | Пройденные и закрытые | Запрет BusinessMutation на закрытых заявках. Разрешены SystemMutation (152-ФЗ) | Защита бизнес-данных |
| **Р8** | Retention Period (Улики) | Файлы хранятся 30 дней в Outbox перед очисткой из S3 | Для расследований |
| **Р9** | Открепление вложений | Любое открепление вложений (через PATCH или удаление коммента) ставит файлы в Outbox | Гарантия очистки без S3 leaks |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и матрица прав доступа
- **Менеджер (КАМ) / Руководитель**: Создание, редактирование, soft-delete.
- **Администратор (Admin)**: Модерация, Scrub (152-ФЗ), скачивание любых файлов (даже удаленных заявок) через обходной Admin API, просмотр истории правок (`comment_versions`).

## 1.2 Контекст шагов эталонного воркфлоу
- **Зона А (Шаги 1–4, 3.1):** `branch_id IS NULL`, `side_pointer_id IS NULL`.
- **Шаг 4.1 (Допсоглашение):** Обязателен `side_pointer_id`, `branch_id IS NULL`.
- **Зона Б (Шаги 5–8):** Обязателен `branch_id`, `side_pointer_id IS NULL`.

## 1.3 Жизненный цикл вложений и Transactional Outbox
1. **Загрузка:** Файл загружается в S3. Записывается в `attachments`.
2. **Привязка:** `INSERT INTO comment_attachments`. При дубликате -> `UniqueViolation` -> 409 Conflict.
3. **Открепление / Soft Delete:** При `DELETE /comments` или удалении файла из массива при `PATCH`. В транзакции создается запись в `attachment_purge_queue` с `execute_after = now() + 30 days`.
4. **Физическая очистка (Worker):** По истечении 30 дней воркер делает: 
   `DELETE FROM comment_attachments WHERE attachment_id = X;`
   `DELETE FROM attachments WHERE id = X;` (удаляет файл из S3). Это разрывает тупик внешних ключей.

---

# Часть II. Технический уровень

## 2.1 Схема базы данных (DDL)

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
    reply_to_comment_id BIGINT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    text TEXT NOT NULL,
    is_edited BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    is_scrubbed BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ NULL,
    deleted_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_comments_text_not_empty CHECK (text ~ '[^\s]'),
    CONSTRAINT chk_comments_author_role CHECK (author_role IN ('manager', 'supervisor', 'admin')),
    CONSTRAINT chk_comments_deleted_fields CHECK (
        (is_deleted = FALSE AND deleted_at IS NULL AND deleted_by IS NULL AND is_scrubbed = FALSE) OR
        (is_deleted = TRUE AND deleted_at IS NOT NULL)
    ),
    -- Коллизия Зон исключена
    CONSTRAINT chk_comments_zones CHECK (branch_id IS NULL OR side_pointer_id IS NULL)
);

-- Индексы для FK (защита от Full Table Scan при блокировках)
CREATE INDEX ix_comments_interaction_id ON comments(interaction_id);
CREATE INDEX ix_comments_branch_id ON comments(branch_id);
CREATE INDEX ix_comments_side_pointer_id ON comments(side_pointer_id);
CREATE INDEX ix_comments_reply_to ON comments(reply_to_comment_id);

-- Таблица версий (защита улик)
CREATE TABLE comment_versions (
    id BIGSERIAL PRIMARY KEY,
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    previous_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_comment_versions_comment_id ON comment_versions(comment_id);

CREATE TABLE comment_attachments (
    comment_id BIGINT NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (comment_id, attachment_id),
    CONSTRAINT uq_comment_attachments_attachment_id UNIQUE (attachment_id)
);
CREATE INDEX ix_comment_attachments_comment_id ON comment_attachments(comment_id);

CREATE TABLE attachment_purge_queue (
    id BIGSERIAL PRIMARY KEY,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
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
| **C21**| `reply_to_comment_id = R ⇒ R.interaction_id == comment.interaction_id ∧ R.branch_id == comment.branch_id ∧ R.side_pointer_id == comment.side_pointer_id ∧ R.is_deleted = FALSE` | Service |
| **C22**| `branch_id = B ⇒ B.interaction_id == comment.interaction_id` | Service |
| **C23**| `side_pointer_id = P ⇒ P.interaction_id == comment.interaction_id` | Service |

## 2.3 Транзакционность: Strict Lock Hierarchy
Действует на **все** мутации (`POST`, `PATCH`, `DELETE`):
1. `Stateless Read`: сбор ключей без блокировки.
2. `Lock Parents`: 
   `SELECT * FROM interactions FOR SHARE`
   `SELECT * FROM branches FOR SHARE`
   `SELECT * FROM side_pointers FOR SHARE`
3. `Lock Child`: `SELECT * FROM comments FOR UPDATE`.

## 2.4 DTO
Использование `set[int]`.
```python
class CommentCreate(BaseModel):
    attachment_ids: Annotated[set[int], Field(default_factory=set, max_length=10)]
```
