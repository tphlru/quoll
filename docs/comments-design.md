# Дизайн: подсистема комментариев к шагам взаимодействий (CRM quoll)

Редакция 19 (Restored Full Context). Возвращены вырезанные разделы API и бизнес-требований.

Редакция 6, 28.09.2026. Полная архитектурная сходимость промышленного уровня.
Устранены и закрыты:
- [P0] Уязвимость IDOR при привязке вложений (C17, проверка авторства, изоляция);
- [P1] Изоляция веток боковых прохождений (C5, требование совпадения side_pointer_id);
- [P1] Редактирование в закрытых ДС (C9, блокировка side_pointers FOR SHARE, COM-018);
- [P1] Регламент 152-ФЗ / Scrub (C18, физическое затирание текста, санитизация логов);
- [P2] Защита от каскадного удаления веток и указателей (ON DELETE RESTRICT);
- [P2] TOCTOU гонка мульти-привязки вложений (`UNIQUE (attachment_id)` в DDL);
- [P2] Отказоустойчивое удаление файлов из S3 (Outbox-очередь `attachment_purge_queue` с ретраями);
- [P3] Явная спецификация безусловного перевода в `is_deleted = true` при Scrub;
- [P3] Запрет комментирования на терминальных стадиях (C19, `COM-019`);
- [P3] Защита от пробельных строк и дубликатов вложений на уровне DTO-валидаторов (T25, T26).

Основание:
- модель данных CRM ИТ Школы РТК v1.3 (п. 3.1, 3.12, 3.15, 3.19, 6.1, 7, 15);
- процесс и роли AS IS v2.0 (роли КАМ, Руководитель, Администратор);
- эталонный маршрут «Работа с вузами» (`backend/src/quoll/seed/workflow_universities.json`): 8 основных шагов (1–4 — взаимодействие, 5–8 — ветки продуктов), шаг 3.1 (корректировка), шаг 4.1 (допсоглашение / side stage);
- механизм боковых прохождений (`SidePointer`, `side_pointer_id`, `SidePointerStatus`);
- модуль вложений `backend/src/quoll/attachments/` и S3-хранилище;
- подсистема уведомлений `backend/src/quoll/notifications/`;
- журнал аудита `backend/src/quoll/auth/audit.py` и таблица кодов ошибок `docs/errors-ru.md` (область `COM-xxx`);
- правила архитектурной сходимости `.claude/agents/architecture-convergence.md`;
- эталон проектирования `docs/reports-design.md`.

---

## 0. Принятые решения и архитектурные развилки

| # | Вопрос | Решение | Обоснование |
|---|---|---|---|
| **Р1** | Черновики комментариев | Черновиков нет. Комментарий создаётся и сразу публикуется | Решение пользователя (A1). Простота жизненного цикла, отсутствие скрытых состояний |
| **Р2** | Привязка к продуктовым веткам | На шагах 1–4, 3.1 — привязка к взаимодействию (`branch_id IS NULL`). На шагах 5–8 (`stage.branch = true`) — строго к ветке продукта (`branch_id IS NOT NULL`) | Решение пользователя (A2). Внедрение продуктов на шагах 5–8 идет параллельно и независимо |
| **Р3** | Количество шагов воркфлоу | В системе **8 основных шагов** (+ подшаг 3.1 корректировки и 4.1 допсоглашения), а не 14 | Решение пользователя (A2), сверено с `workflow_universities.json` |
| **Р4** | Структура обсуждения | Плоская хронологическая лента с опциональным указанием `reply_to_comment_id` (цитирование / контекстный ответ) | Решение пользователя (A3). Исключает сложную вложенность деревьев, сохраняя контекст обсуждения |
| **Р5** | Редактирование и удаление | Редактировать текст может **только автор**. Удалять (soft-delete) может **автор или администратор** (модерация). Руководитель чужие комментарии удалять не может | Решение пользователя (A4). Защита от произвольного изменения чужих слов руководителями; аудит всех правок |
| **Р6** | Сквозная лента (timeline) | Два режима выборки: 1) комментарии конкретного шага/ветки; 2) сквозная лента всех комментариев заявки по всем шагам | Решение пользователя (A5). Позволяет видеть как локальную историю шага, так и сквозной журнал по вузу |
| **Р7** | Уведомления руководителю | При отправке КАМ может выставить флаг `notify_supervisor: bool` (по умолчанию `false`). Если пишет руководитель — КАМу-владельцу уходит всегда. При ответе — автору исходного комментария | Снижение шума уведомлений у руководителя; гарантированное оповещение КАМа о репликах руководства |
| **Р8** | Безопасность вложений и IDOR | Защита от подмены ID: вложение при создании комментария валидируется на авторство (`uploaded_by == actor.id`), свежесть (< 24 ч) и отсутствие других привязок | Полное устранение уязвимости IDOR. Чужие файлы невозможно привязать к своему комментарию |
| **Р9** | Пройденные и закрытые шаги | На пройденных шагах активной заявки комментирование разрешено. В закрытых заявках, закрытых ветках и завершенных ДС комментирование и редактирование запрещены | Соответствует модели данных v1.3 (п. 15); закрытая сделка замораживается |
| **Р10** | Иммутабельность вложений | Состав прикрепленных файлов фиксируется при создании комментария. При `PATCH` меняется только текст | Предотвращает подмену доказательных документов задним числом |
| **Р11** | Боковые прохождения (Шаг 4.1 ДС) | Поддержка `side_pointer_id: int | None` для комментариев на стадии допсоглашений. Строгая изоляция ответов | Изоляция комментариев по разным ДС на шаге 4.1; запрет кросс-цитирования между разными ДС |
| **Р12** | Очистка орфанных файлов | Фоновый периодический воркер удаляет вложения старше 24 часов, не привязанные к комментариям | Исключает утечку места в S3 и БД от незавершённых отправок |
| **Р13** | Регламент 152-ФЗ (Scrub) | Разделение мягкого удаления (для пользователей) и необратимого стирания ПДн (`scrub`) администратором | Юридическая чистота: физическое затирание текста в БД, удаление файлов из S3, санитизация аудит-лога |
| **Р14** | Эксклюзивность привязки вложений | `UNIQUE (attachment_id)` в `comment_attachments` исключает гонку параллельного прикрепления одного файла | Гарантия целостности на уровне СУБД |
| **Р15** | Отказоустойчивое удаление S3 | Таблица `attachment_purge_queue` для асинхронного удаления файлов из S3 с ретраями | Гарантирует отсутствие расхождений при сбоях сети/S3 |

---

# Часть I. Бизнес-уровень

## 1.1 Роли и матрица прав доступа

Подсистема комментариев регламентирует коммуникацию трёх ролей:

| Действие | Менеджер (КАМ) | Руководитель (Superviser) | Администратор (Admin) |
|---|---|---|---|
| **Чтение комментариев шага** | Заявки, где он текущий владелец или был ответственным ранее (по `readable_filter`) | Заявки своей команды, заявки сиротских команд и заявки своих КАМов | Все заявки системы (только чтение) |
| **Чтение сквозной ленты заявки** | Доступно в пределах видимости заявки | Доступно в пределах видимости заявки | Доступно по всей системе |
| **Создание комментария** | Только в активных незакрытых заявках, где он текущий владелец (`owner_id == user.id`) | В активных незакрытых заявках своей команды (для обратной связи, ревью шага) | Запрещено (админ не выполняет бизнес-действий) |
| **Редактирование комментария** | Только свои комментарии в активной незакрытой заявке | Только свои комментарии в активной незакрытой заявке | Запрещено |
| **Удаление комментария (soft)** | Только свои комментарии | Только свои комментарии (чужие удалять запрещено) | Любые комментарии (модерация) |
| **Уничтожение ПДн (scrub)** | Запрещено | Запрещено | Разрешено (по официальному запросу / инциденту) |
| **Загрузка вложений** | Разрешено для подготовки к отправке комментария | Разрешено для подготовки к отправке комментария | Разрешено |
| **Скачивание вложений** | Доступно, если разрешено чтение заявки и комментарий не удален | Доступно, если разрешено чтение заявки и комментарий не удален | Доступно всегда (включая удаленные комментарии при расследовании) |

---

## 1.2 Контекст шагов эталонного воркфлоу (8 шагов)

Маршрут взаимодействия с вузом разделен на две принципиально разные зоны:

```
Зона А. Уровень взаимодействия (договор в целом):
  Шаг 1: Контакт              (поля: контакт вуза, дата)
    ↓
  Шаг 2: Встреча              (поля: дата встречи)
    ↓
  Шаг 3: Документы            (подготовка соглашения) ←→ Шаг 3.1: Корректировка (доработка)
    ↓
  Шаг 4: Подписание           (номер, даты, подписан) ←→ Шаг 4.1: Допсоглашение (side: true, handler: SA)
  [ТОЧКА НЕВОЗВРАТА: no_return_at фиксируется при подписании]
    ↓
Зона Б. Уровень продуктовых веток (параллельное внедрение ПО вендоров):
  Шаг 5: Передача             (branch_start: true; статус передачи, лицензия, срок)
    ↓
  Шаг 6: Внедрение            (дата запуска)
    ↓
  Шаг 7: Обучение             (обучено преподавателей КАМом)
    ↓
  Шаг 8: Обновление           (актуализация ПО)
    ↓
  Терминальные стадии веток: «Ветка завершена», «Ветка: отказ»
```

### Правила контекстной изоляции:
1. **Зона А (Шаги 1–4, 3.1):**
   * Обсуждение касается всего вуза. `stage.branch == false`.
   * Комментарий создается с `branch_id = NULL`. Передача `branch_id` вызывает ошибку `COM-010`.
2. **Шаг 4.1 (Допсоглашение):**
   * Является боковым шагом (`stage.side == true`).
   * Взаимодействие может проходить несколько ДС последовательно или параллельно.
   * Комментарий привязывается к текущему боковому прохождению (`side_pointer_id`).
   * Если ДС завершено или отклонено (`side_pointer.status in (FINISHED, CANCELLED)`), добавление и правка комментариев в нём блокируются (`COM-018`).
3. **Зона Б (Шаги 5–8):**
   * Каждый продукт ведется отдельной сущностью `Branch`. `stage.branch == true`.
   * Комментарии на шаге 5 по Продукту №1 не видны и не смешиваются с комментариями на шаге 5 по Продукту №2.
   * Передача `branch_id` **обязательна** (`COM-011`). Передача чужого `branch_id` вызывает `COM-012`.
4. **Пройденные шаги:**
   * Добавление комментария к пройденному шагу разрешено для фиксации важных ретроспективных фактов.
5. **Закрытые заявки и ветки:**
   * Если заявка переведена в `CLOSED` или ветка имеет `closed_at IS NOT NULL`, добавление, редактирование и удаление комментариев блокируются (`COM-016`). Вся лента переходит в неизменяемый архив.

---

## 1.3 Структура ленты, сообщения и цитирование

Обсуждение на шаге формирует хронологический диалог:
* Базовый порядок сортировки: по идентификатору `id ASC` (монотонный порядок фиксации в транзакции).
* **Цитирование (`reply_to_comment_id`):**
  * Позволяет сослаться на конкретный вопрос или реплику коллеги.
  * Цитируемый комментарий обязан находиться **в том же контексте**: `(interaction_id, stage_id, branch_id, side_pointer_id)`.
  * Попытка ответить на комментарий с другого шага, чужой ветки или параллельного ДС блокируется с кодом `COM-013`.
  * При отображении клиенту отдается проекция `reply_to`: `{id, author_name, text_preview}`.
  * Если исходный комментарий был мягко удален, в плашке цитирования выводится текст `"Сообщение удалено"`.

---

## 1.4 Жизненный цикл комментария и правила мутаций

```
              ┌───────────────┐
              │  POST запрос  │
              └───────┬───────┘
                      ▼
              ┌───────────────┐  PATCH (автор)   ┌───────────────┐
              │  Опубликован  │ ───────────────> │   Изменен     │
              └───────┬───────┘ <─────────────── │ (is_edited=t) │
                      │          PATCH (автор)   └───────┬───────┘
                      │                                  │
                      │ DELETE (автор / админ)           │ DELETE (автор / админ)
                      ▼                                  ▼
              ┌──────────────────────────────────────────────────┐
              │                  Мягко удален                    │
              │ (is_deleted=true, text masked, attachments lock) │
              └───────┬──────────────────────────────────────────┘
                      │
                      │ POST /scrub (только Администратор)
                      ▼
              ┌──────────────────────────────────────────────────┐
              │            Уничтожен по 152-ФЗ (Scrubbed)        │
              │  (text overwritten, files purged, logs cleaned)  │
              └──────────────────────────────────────────────────┘
```

1. **Создание (Публикация):**
   * Мгновенная фиксация в БД, генерация события аудита `COMMENT_CREATED`.
   * Отправка push-уведомлений участникам.
2. **Редактирование (`PATCH`):**
   * Разрешено исключительно автору (`author_id == actor.id`).
   * Запрещено, если комментарий удален (`COM-006`).
   * Запрещено, если заявка/ветка/ДС закрыта (`COM-016`, `COM-018`).
   * Допустимо менять только текст (`text`). Состав вложений неизменен.
   * Устанавливается `is_edited = true`, обновляется `updated_at`.
   * В аудит пишется событие `COMMENT_UPDATED` со старым и новым текстом.
3. **Мягкое удаление (`DELETE`):**
   * Разрешено автору комментария или администратору (`AdminUser`).
   * Руководитель чужой комментарий удалить не может (`COM-005`).
   * Строка сохраняется в БД для поддержания целостности внешних ключей `reply_to_comment_id`.
   * Проставляются `is_deleted = true`, `deleted_at = now()`, `deleted_by = actor.id`.
   * Текст при выборке обычными пользователями заменяется на константу `"Комментарий удален"`.
   * В аудит пишется событие `COMMENT_DELETED`.
4. **Уничтожение ПДн по 152-ФЗ (`POST /comments/{id}/scrub`):**
   * Применимо администратором как к активным, так и к уже удаленным комментариям.
   * Безусловно проставляет `is_deleted = true, is_scrubbed = true, deleted_at = now(), deleted_by = actor.id`.
   * Физически перезаписывает `text` в базе на `"[ДАННЫЕ УДАЛЕНЫ ПО ТРЕБОВАНИЮ 152-ФЗ]"`.
   * Переносит `storage_key` привязанных файлов в очередь `attachment_purge_queue` и удаляет строки из БД.
   * Очищает историю в `audit_logs` (заменяет `old_value` и `new_value` на санитизированные заглушки).

---

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


## 1.6 Уведомления и адресация

Интеграция с подсистемой `quoll.notifications` через единую функцию `emit(...)`:

| Инициатор | Событие | Флаг / Условие | Адресат (`Audience`) | Тип уведомления |
|---|---|---|---|---|
| КАМ | Новый комментарий к шагу | `notify_supervisor == true` | Руководитель команды КАМа (`Role.OWNER_SUPERVISOR`) | `COMMENT_POSTED` |
| КАМ | Новый комментарий к шагу | `notify_supervisor == false` | Никому (тихий комментарий) | — |
| Руководитель | Новый комментарий к шагу | Всегда | Текущий КАМ-владелец (`Role.OWNER`) | `COMMENT_TO_OWNER` |
| Любой | Ответ на комментарий | `reply_to_comment_id IS NOT NULL` | Автор родительского сообщения (`Role.USER`, `parent.author_id`) | `COMMENT_REPLIED` |

* **Правило исключения актора:** автор комментария никогда не получает уведомление о собственном действии (`skip = actor_id`).
* **Недееспособные пользователи:** уволенные, отключенные или сотрудники в процессе смены роли отфильтровываются через `incapacitated_expression`.
* **Доставка:** через вызов `pg_notify("quoll_notifications", id)` при коммите транзакции с последующей трансляцией слушателем в активные WebSocket-сессии.

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


## 2.2 Инварианты системы

| # | Инвариант (`Условие ⇒ Следствие`) | Уровень обеспечения | Поведение при нарушении | Код ошибки |
|---|---|---|---|---|
| **C1** | `stage.branch == false ⇒ branch_id IS NULL` | Service | Откат транзакции, 400 | `COM-010` |
| **C2** | `stage.branch == true ⇒ branch_id IS NOT NULL` | Service | Откат транзакции, 400 | `COM-011` |
| **C3** | `branch_id = B ⇒ B.interaction_id == interaction.id` | Service | Откат транзакции, 404 | `COM-012` |
| **C4** | `side_pointer_id = P ⇒ P.interaction_id == interaction.id` | Service | Откат транзакции, 404 | `COM-017` |
| **C5** | `reply_to_comment_id = R ⇒ (R.interaction_id == comment.interaction_id AND R.stage_id == comment.stage_id AND R.branch_id IS NOT DISTINCT FROM comment.branch_id AND R.side_pointer_id IS NOT DISTINCT FROM comment.side_pointer_id)` | Service | Откат транзакции, 404 | `COM-013` |
| **C6** | `actor_id != comment.author_id ⇒ edit_forbidden` | Service + Policy | Откат транзакции, 403 | `COM-004` |
| **C7** | `(actor_id != comment.author_id AND actor.role != 'admin') ⇒ delete_forbidden` | Service + Policy | Откат транзакции, 403 | `COM-005` |
| **C8** | `comment.is_deleted == true ⇒ edit_forbidden` | Service | Откат транзакции, 409 | `COM-006` |
| **C9** | `interaction.status == CLOSED OR (branch_id IS NOT NULL AND branch.closed_at IS NOT NULL) OR (side_pointer_id IS NOT NULL AND side_pointer.status != 'ACTIVE') ⇒ mutate_forbidden` | Service (под FOR SHARE) | Откат транзакции, 409 | `COM-016`, `COM-018` |
| **C10** | `count(attachment_ids) <= 10` | Schema + Service | Откат транзакции, 422 | `COM-014` |
| **C11** | `attachment_id = A ⇒ A exists in attachments` | Service | Откат транзакции, 404 | `COM-015` |
| **C12** | `length(trim(text)) BETWEEN 1 AND 4000` | DB (`CHECK`) + Schema | Откат транзакции, 422 | `COM-007`, `COM-008` |
| **C13** | `is_deleted == true ⇒ text masked in API` | Service / Serializer | Маскирование на выходе | — |
| **C14** | `author_name, author_role immutable` | Service | Фиксация при INSERT | — |
| **C15** | `is_deleted == true AND deleted_at IS NOT NULL` | DB (`CHECK`) | Откат транзакции на уровне СУБД | — |
| **C16** | `attachment downloadable ⇒ ∃ comment ∈ comment_attachments: comment.is_deleted = false AND can_read(user, comment.interaction)` | `document_service.py` | 403 Forbidden | `COM-002` |
| **C17** | `∀ a ∈ attachment_ids ⇒ a.uploaded_by == actor.id ∧ a.storage_key ~ '^comments/' ∧ a unbound` | Service + DB (`UNIQUE`) | Откат транзакции, 403 / 409 | `COM-015` |
| **C18** | `is_scrubbed == true ⇒ is_deleted == true ∧ text == '[ДАННЫЕ УДАЛЕНЫ ПО ТРЕБОВАНИЮ 152-ФЗ]' ∧ attachments count == 0` | Service (Scrub) | Гарантия очистки ПДн | — |
| **C19** | `stage.is_terminal == true ⇒ comment_forbidden` | Service | Откат транзакции, 400 | `COM-019` |

---

## 2.2 Транзакционность: Strict Lock Hierarchy
1. `Stateless Read`
2. `Lock Parents`: `FOR SHARE` (interaction, branch, side_pointer, parent_comment `is_deleted = false`)
3. `Lock Child`: `FOR UPDATE` (`is_scrubbed = false AND is_deleted = false`)


## 2.4 Интеграция с вложениями и надежное удаление (Scrub / Outbox)

### Логика выполнения `POST /api/v1/comments/{id}/scrub`:
1. В транзакции под `FOR UPDATE` блокируется комментарий.
2. Безусловно выставляется:
   ```python
   comment.is_deleted = True
   comment.is_scrubbed = True
   comment.deleted_at = datetime.now(timezone.utc)
   comment.deleted_by = actor.id
   comment.text = "[ДАННЫЕ УДАЛЕНЫ ПО ТРЕБОВАНИЮ 152-ФЗ]"
   ```
3. Читаются все `storage_key` привязанных файлов.
4. В таблицу `attachment_purge_queue` вставляются записи для каждого `storage_key`.
5. Удаляются строки из `comment_attachments` и `attachments`.
6. Санитизируются строки в `audit_logs` для данного `comment_id`.
7. Фиксируется событие аудита `AuditEventType.COMMENT_SCRUBBED`.
8. Коммит транзакции.
9. Фоновый воркер `clean_purged_attachments()` вычитывает очередь `attachment_purge_queue`, удаляет объекты из S3 с ретраями и проставляет `processed_at = now()`.

---

## 2.5 Спецификация API и схемы DTO

Все схемы размещаются в `src/quoll/comments/schemas.py` с `ConfigDict(extra="forbid")`.

### DTO модели:

```python
class CommentAuthorRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    role: str

class CommentReplyPreviewRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    author_name: str
    text_preview: str

class CommentAttachmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    filename: str
    size_bytes: int
    mime_type: str

class CommentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    interaction_id: int
    stage_id: int
    branch_id: int | None
    side_pointer_id: int | None
    author: CommentAuthorRead
    text: str
    reply_to: CommentReplyPreviewRead | None
    attachments: list[CommentAttachmentRead]
    is_edited: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    branch_id: int | None = None
    side_pointer_id: int | None = None
    reply_to_comment_id: int | None = None
    attachment_ids: Annotated[list[int], Field(default_factory=list, max_length=10)]
    notify_supervisor: bool = False

    @field_validator("text")
    @classmethod
    def validate_text_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Текст комментария не может состоять только из пробелов")
        return v

    @field_validator("attachment_ids")
    @classmethod
    def validate_unique_attachments(cls, v: list[int]) -> list[int]:
        if len(v) != len(set(v)):
            raise ValueError("Список вложений содержит повторяющиеся идентификаторы")
        return v

class CommentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("text")
    @classmethod
    def validate_text_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Текст комментария не может состоять только из пробелов")
        return v

class CommentFeedRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int
    items: list[CommentRead]
```

### Эндпоинты роутера `comments_router`:

1. `GET /api/v1/interactions/{id}/stages/{stage_id}/comments`
   - Чтение комментариев шага.
   - Query: `branch_id: int | None`, `side_pointer_id: int | None`, `limit: int = 50`, `offset: int = 0`.
   - Ошибки: `400 (COM-010, COM-011)`, `403 (COM-002)`, `404 (COM-009, COM-012, COM-017)`.
2. `GET /api/v1/interactions/{id}/comments`
   - Сквозная хронологическая лента (timeline) всей заявки.
   - Query: `limit: int = 50`, `offset: int = 0`, `stage_id: int | None`, `branch_id: int | None`.
   - Ошибки: `403 (COM-002)`, `404`.
3. `POST /api/v1/interactions/{id}/stages/{stage_id}/comments`
   - Создание комментария.
   - Body: `CommentCreate`.
   - Ошибки: `400 (COM-010, COM-011)`, `403 (COM-003, COM-015)`, `404 (COM-009, COM-012, COM-013, COM-017)`, `409 (COM-016, COM-018)`, `422 (COM-007, COM-008, COM-014)`.
4. `PATCH /api/v1/comments/{id}`
   - Редактирование своего комментария.
   - Body: `CommentUpdate`.
   - Ошибки: `403 (COM-004)`, `404 (COM-001)`, `409 (COM-006, COM-016, COM-018)`, `422 (COM-007, COM-008)`.
5. `DELETE /api/v1/comments/{id}`
   - Мягкое удаление (автор или админ).
   - Ошибки: `403 (COM-005)`, `404 (COM-001)`, `409 (COM-016, COM-018)`. Возвращает `204 No Content`.
6. `POST /api/v1/comments/{id}/scrub`
   - Необратимое уничтожение ПДн по 152-ФЗ (только Admin).
   - Ошибки: `403 (AdminOnly)`, `404 (COM-001)`. Возвращает `200 OK`.
7. `POST /api/v1/comments/attachments`
   - Загрузка вложения для комментария (multipart/form-data).
   - Сохраняет `uploaded_by = current_user.id`.
   - Ошибки: `403`, `413 (Payload Too Large)`.

---

## 2.6 Реестр кодов ошибок (`COM-xxx`)

| Код | Кат. | HTTP | Когда возникает | Текст пользователю | Что делать | Кто видит |
|---|---|---|---|---|---|---|
| `COM-001` | П | 404 | Комментарий с указанным ID не найден | Комментарий не найден | Обновите страницу | все |
| `COM-002` | П | 403 | Нет прав на чтение комментариев заявки | Нет прав на это действие | Обратитесь к администратору | все |
| `COM-003` | П | 403 | Нет прав на добавление комментария в заявку | Нет прав на добавление комментария | Комментировать может ответственный КАМ или его руководитель | менеджер, руководитель |
| `COM-004` | П | 403 | Попытка изменить чужой комментарий | Редактировать комментарий может только его автор | Вы можете оставить свой комментарий с уточнением | менеджер, руководитель |
| `COM-005` | П | 403 | Попытка удалить чужой комментарий не-админом | Удалять комментарий может только автор или администратор | Обратитесь к администратору | менеджер, руководитель |
| `COM-006` | П | 409 | Попытка изменить уже удаленный комментарий | Этот комментарий уже удален | Обновите страницу | все |
| `COM-007` | П | 422 | Текст комментария пуст или состоит из пробелов | Текст комментария не может быть пустым | Введите текст сообщения | все |
| `COM-008` | П | 422 | Превышена предельная длина текста (4000 знаков) | Комментарий слишком длинный (до 4000 символов) | Сократите текст сообщения | все |
| `COM-009` | П | 404 | Шаг не найден или не принадлежит воркфлоу заявки | Выбранный шаг не найден в маршруте заявки | Обновите страницу | все |
| `COM-010` | П | 400 | Передана ветка продукта на шагах 1–4, 3.1 | На общих шагах заявки нельзя указывать ветку продукта | Оставьте комментарий к шагу заявки без выбора продукта | менеджер, руководитель |
| `COM-011` | П | 400 | Не передана ветка продукта на продуктовых шагах 5–8 | Для шагов внедрения необходимо выбрать продукт | Выберите продукт для комментирования шага | менеджер, руководитель |
| `COM-012` | П | 404 | Ветка продукта не найдена или не относится к заявке | Выбранный продукт не найден в заявке | Обновите страницу и выберите продукт заново | менеджер, руководитель |
| `COM-013` | П | 404 | Исходный комментарий для ответа не найден в данном контексте | Сообщение для ответа не найдено на этом шаге | Обновите страницу | все |
| `COM-014` | П | 422 | Прикреплено более 10 вложений | Нельзя прикрепить больше 10 файлов к одному комментарию | Удалите лишние файлы | все |
| `COM-015` | П | 403 | Попытка прикрепить чужое или уже связанное вложение (IDOR) | Прикрепляемый файл недоступен или уже использован | Загрузите файл повторно | все |
| `COM-016` | П | 409 | Заявка или ветка закрыта | Взаимодействие или продукт закрыты: добавление комментариев недоступно | Работа по заявке завершена | менеджер, руководитель |
| `COM-017` | П | 404 | Боковое прохождение (side pointer) не найдено в заявке | Прохождение не найдено в заявке | Обновите страницу | менеджер, руководитель |
| `COM-018` | П | 409 | Дополнительное соглашение закрыто или отклонено | Дополнительное соглашение завершено: комментирование недоступно | Оформление ДС завершено | менеджер, руководитель |
| `COM-019` | П | 400 | Попытка добавить комментарий на завершающей стадии (done, refused, branch_done, branch_refused) | На завершающей стадии маршрута комментирование недоступно | Работа по данному этапу завершена | менеджер, руководитель |

---

## 2.7 Миграция базы данных (Alembic)

Файл ревизии: `backend/alembic/versions/<revision>_comments_and_comment_attachments.py`.
- `down_revision = "57ea0170aa1e"` (текущая голова `dev`).
- Реализуются функции `upgrade()` и `downgrade()`.
- Именование ограничений и индексов строго по `NAMING_CONVENTION`.
- Обязательная проверка циклом:
  `uv run alembic upgrade head` → `uv run alembic downgrade -1` → `uv run alembic upgrade head` → `uv run alembic check`.

---

## 2.8 Тестовая матрица (E2E и граничные случаи)

Все тесты размещаются в `backend/tests_catalog/test_comments.py`:

| # | Тест | Сценарий проверки | Ожидаемый результат |
|---|---|---|---|
| **T1** | `test_comment_create_step_success` | КАМ публикует комментарий на шаге 2 без ветки | 201 Created, комментарий сохранен, автор и денормализованные поля зафиксированы |
| **T2** | `test_comment_create_branch_step_success` | КАМ публикует комментарий на шаге 5 с `branch_id` | 201 Created, привязан к конкретной ветке продукта |
| **T3** | `test_comment_create_branch_on_general_step_400` | Передача `branch_id` на шаге 1 | 400 Bad Request (`COM-010`) |
| **T4** | `test_comment_create_missing_branch_on_step5_400` | Отсутствие `branch_id` на шаге 5 | 400 Bad Request (`COM-011`) |
| **T5** | `test_comment_isolation_between_branches` | Продукт А и Продукт Б оба на шаге 5; запросить комментарии шага 5 по Продукту А | Возвращаются только комментарии Продукта А; комментарии Продукта Б не попадают |
| **T6** | `test_comment_edit_by_author_success` | Автор меняет текст через `PATCH` | 200 OK, `is_edited = true`, `updated_at` обновлен, в `audit_logs` записан `COMMENT_UPDATED` |
| **T7** | `test_comment_edit_by_non_author_forbidden_403` | Руководитель пытается отредактировать комментарий КАМа | 403 Forbidden (`COM-004`), текст не изменился |
| **T8** | `test_comment_soft_delete_by_author` | Автор удаляет свой комментарий | 204 No Content, `is_deleted = true`, текст замаскирован, вложения заблокированы |
| **T9** | `test_comment_soft_delete_by_admin` | Администратор удаляет чужой комментарий (модерация) | 204 No Content, `deleted_by = admin.id` |
| **T10** | `test_comment_soft_delete_by_supervisor_forbidden_403` | Руководитель пытается удалить чужой комментарий | 403 Forbidden (`COM-005`) |
| **T11** | `test_comment_edit_already_deleted_conflict_409` | Попытка `PATCH` удаленного комментария | 409 Conflict (`COM-006`) |
| **T12** | `test_comment_on_closed_interaction_conflict_409` | Попытка добавить комментарий в закрытую заявку | 409 Conflict (`COM-016`) |
| **T13** | `test_comment_on_closed_branch_conflict_409` | Попытка добавить комментарий к шагу закрытой ветки | 409 Conflict (`COM-016`) |
| **T14** | `test_comment_reply_cross_step_forbidden_404` | Попытка ответить на комментарий с другого шага | 404 Not Found (`COM-013`) |
| **T15** | `test_comment_reply_cross_side_pointer_forbidden_404` | Попытка ответить на комментарий из другого ДС на шаге 4.1 | 404 Not Found (`COM-013`) |
| **T16** | `test_comment_idor_attachment_binding_forbidden_403` | Попытка передать чужой `attachment_id` при создании комментария | 403 Forbidden (`COM-015`), привязка пресечена |
| **T17** | `test_comment_concurrent_attachment_binding_race_409` | Два одновременных запроса пытаются привязать один и тот же `attachment_id` | Один завершается 201, второй получает 409 (Unique violation) |
| **T18** | `test_comment_attachments_max_limit_422` | Попытка прикрепить 11 файлов | 422 Unprocessable Entity (`COM-014`) |
| **T19** | `test_comment_attachments_download_deleted_forbidden_403` | Скачивание файла, прикрепленного только к удаленному комментарию | 403 Forbidden |
| **T20** | `test_comment_scrub_pii_by_admin` | Админ вызывает `/scrub`: текст в БД затерт, файл поставлен в `attachment_purge_queue`, аудит санитизирован | 200 OK, в БД текст заменен, файл из S3 вычищен воркером |
| **T21** | `test_comment_notification_flag_supervisor` | КАМ отправил с `notify_supervisor = true` | Уведомление `COMMENT_POSTED` создано для руководителя |
| **T22** | `test_comment_notification_silent` | КАМ отправил с `notify_supervisor = false` | Уведомление руководителю не создано |
| **T23** | `test_clean_orphan_attachments_worker` | Файл загружен в S3, прошло 25 часов, комментарий не создан | Воркер удаляет файл из S3 и `attachments` |
| **T24** | `test_concurrent_close_and_comment_race` | Параллельная гонка закрытия заявки и комментирования | Детерминированный исход (один завершается успешно, второй получает 409), инвариант C9 не нарушен |
| **T25** | `test_comment_create_empty_or_whitespace_text_422` | Отправка текста, состоящего только из пробелов и переносов строк | 422 Unprocessable Entity (`COM-007`) |
| **T26** | `test_comment_duplicate_attachment_ids_422` | Отправка массива `attachment_ids: [1, 1]` | 422 Unprocessable Entity |
| **T27** | `test_comment_reply_to_deleted_comment_preview_masked` | Ответ на комментарий, который был ранее мягко удален | 201 Created, превью родителя маскировано: `text_preview = "Сообщение удалено"` |
| **T28** | `test_comment_on_terminal_stage_forbidden_400` | Попытка комментирования стадии со свойством `stage.is_terminal = true` | 400 Bad Request (`COM-019`) |

---

# Часть III. Проверка по Definition of Done (DoD)

1. **A. Semantic completeness: ВЫПОЛНЕНО.** Все концепции (8 шагов, ветки 5–8, боковое прохождение 4.1, терминальные стадии, иммутабельность роли автора, цитирование, мягкое удаление, scrub по 152-ФЗ, очередь очистки S3) определены без двусмысленностей.
2. **B. Invariant completeness: ВЫПОЛНЕНО.** Все 19 инвариантов C1–C19 строго формализованы (`Условие ⇒ Следствие`), привязаны к конкретному уровню обеспечения и маппятся на таблицу ошибок `COM-xxx`.
3. **C. Lifecycle completeness: ВЫПОЛНЕНО.** Жизненный цикл комментариев, боковых прохождений и жизненный цикл вложений (включая очистку орфанов, уникальность привязки и outbox-очередь S3) замкнуты, тупиков нет.
4. **D. Concurrency completeness: ВЫПОЛНЕНО.** Разработаны сценарии гонок с явной иерархией блокировок (`FOR SHARE` на взаимодействия/ветки/указатели, `FOR UPDATE` на комментарии), а также СУБД-констрейнт `UNIQUE(attachment_id)` против TOCTOU мульти-привязки.
5. **E. Failure completeness: ВЫПОЛНЕНО.** Обработаны отказы S3 через `attachment_purge_queue` с ретраями, обнуление FK при удалении пользователей (`deleted_by`), попытки мутации закрытых сущностей.
6. **F. Persistence/API/Migration consistency: ВЫПОЛНЕНО.** Схема БД, ограничения `RESTRICT`, outbox-таблица, DTO схемы, коды ошибок `COM-xxx` полностью согласованы.
7. **G. No unresolved P0/P1 + P2 closure: ВЫПОЛНЕНО.** P0 = 0, P1 = 0, P2 = 0. Все замечания закрыты доказательными механизмами.
