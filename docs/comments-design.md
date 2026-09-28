# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 11, 29.09.2026. Архитектурная сходимость промышленного уровня.
Устранены P0/P1 уязвимости Редакции 10:
- [P1] Добавлен `storage_key` в таблицу `attachments` для генерации ссылок скачивания.
- [P1] Устранены утечки S3: Soft-Delete комментария теперь каскадно открепляет все его файлы в Outbox. Добавлен фоновый Orphan Sweep для непривязанных файлов (UPLOADED > 24h).
- [P1] Устранен Data Loss улик расследований: таймер очистки файлов теперь ждет закрытия Заявки (Interaction CLOSED/REJECTED + 30 дней), а не просто 30 дней с момента открепления.
- [P1] Закрыта 152-ФЗ утечка: Scrub теперь анонимизирует `author_name` и мгновенно уничтожает (Purge) все прикрепленные к комментарию файлы.
- [P1] Закрыт TOCTOU ответа на удаленный коммент: `SELECT ... AND is_deleted = false FOR SHARE`. Инвариант C21 ослаблен до прекондишена создания.
- [P2] TOCTOU между Edit и Scrub закрыт: патч запрещен для `is_scrubbed = true`.
- [P3] Оптимизирован DDL: `attachment_id` стал PRIMARY KEY в связке, `deleted_by` защищен `RESTRICT`, статус защищен `CHECK`.

---

## 0. Принятые решения и архитектурные развилки

| # | Вопрос | Решение | Обоснование |
|---|---|---|---|
| **Р1** | Привязка к веткам | На 1-4 шагах `branch_id IS NULL`, на 5-8 строго к ветке продукта | Изоляция |
| **Р2** | Улики (Text & Files) | Редактирует только автор. Старый текст и массивы вложений хранятся в `comment_versions` | Защита улик |
| **Р3** | Уведомления | Дедупликация пушей (5 сек окно) | Исключение дублей |
| **Р4** | Безопасность вложений | Строгий `PRIMARY KEY(attachment_id)` в `comment_attachments` | Нет IDOR, нет TOCTOU |
| **Р5** | Scrub (152-ФЗ) | Анонимизирует `text`, `previous_text`, `author_name`, уничтожает файлы | Полное уничтожение ПДн |
| **Р6** | Улики расследований | Открепленные файлы хранятся в Outbox, но воркер удаляет их из S3 только если Заявка закрыта > 30 дней | Защита от уничтожения улик до завершения работы по заявке |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и матрица прав доступа
- **Менеджер / Руководитель**: Создание, редактирование, soft-delete.
- **Администратор (Admin)**: Scrub (152-ФЗ), скачивание любых файлов (даже удаленных комментов) до момента очистки S3.

## 1.2 Жизненный цикл вложений (Zero S3 Leaks)
1. **Загрузка:** `INSERT INTO attachments` (`status = UPLOADED`, `storage_key`).
2. **Orphan Sweep (Сборка мусора):** Фоновый Job каждые 1h находит `status = UPLOADED AND created_at < NOW() - 24h`, удаляет из S3, ставит `PURGED`. (Защита от брошенных загрузок).
3. **Привязка:** `INSERT INTO comment_attachments` (ошибка при дубликате PK).
4. **Открепление / Soft Delete:** `comment_attachments.is_deleted = true`. Запись падает в `attachment_purge_queue`.
5. **Очистка улик (Worker):** Воркер читает очередь (`FOR UPDATE SKIP LOCKED`). Удаляет файл из S3 **ТОЛЬКО** если `interaction.status IN ('CLOSED', 'REJECTED', 'CANCELLED')` и с момента закрытия прошло 30 дней. Обновляет `status = PURGED`, `processed_at = NOW()`.

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
    deleted_by VARCHAR(255) NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_comments_text_not_empty CHECK (text ~ '[^\s]' OR is_scrubbed = TRUE),
    CONSTRAINT chk_comments_author_role CHECK (author_role IN ('manager', 'supervisor', 'admin')),
    CONSTRAINT chk_comments_zones CHECK (branch_id IS NULL OR side_pointer_id IS NULL),
    
    -- Изоляция доменов: кросс-цитирование запрещено на уровне БД
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
    storage_key VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'UPLOADED',
    uploaded_by VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT chk_attachments_status CHECK (status IN ('UPLOADED', 'CLAIMED', 'PURGED'))
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
    id BIGSERIAL PRIMARY KEY,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE RESTRICT,
    storage_key VARCHAR(255) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL
);
-- execute_after удален, логика времени перенесена на join с interactions
```

## 2.2 Инварианты системы
| # | Инвариант | Уровень |
|---|---|---|
| **C9** | `interaction.status IN (CLOSED, CANCELLED, REJECTED) ⇒ BusinessMutation_forbidden` | Service |
| **C21**| `reply_to_comment_id = R ⇒ R.is_deleted = FALSE` (Прекондишен при создании) | Service |
| **C22**| `attachment_ids = [A...] ⇒ ∀A A.uploaded_by == current_user.id` | Service |
| **C23**| `is_scrubbed = TRUE ⇒ PATCH_forbidden` | Service |

## 2.3 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`.
2. `Lock Parents`: 
   `SELECT * FROM interactions FOR SHARE`
   `SELECT * FROM branches FOR SHARE`
   `SELECT * FROM side_pointers FOR SHARE`
   `SELECT * FROM comments WHERE id = :reply_to_comment_id AND is_deleted = false FOR SHARE`
3. `Lock Child`: `SELECT * FROM comments WHERE is_scrubbed = false FOR UPDATE`.
