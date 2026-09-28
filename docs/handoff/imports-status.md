# Импорт: состояние реализации (28.09.2026)

Ветка `imports`. Дизайн - `docs/import-design.md` (ред. 3, коммит `47fc60a`); реализация идёт строго по нему. Этот коммит - незавершённый коммит 1 из §27.

## Что сделано

| Файл | Что внутри | Состояние |
|---|---|---|
| `backend/alembic/versions/ea8975536d41_import_batches.py` | таблицы `import_batches`, `import_sheets`, `import_rows`, `import_files`; `interaction_documents.attachment_id` NULL + CHECK `chk_document_file_or_contract`; причины `REPLACED_BY_IMPORT*` | цикл вверх-вниз-вверх проходит, `alembic check` чист |
| `backend/alembic/env.py` | импорт `quoll.imports.models` | готово |
| `backend/src/quoll/core/pii.py` | `EncryptedJSON` | готово |
| `backend/src/quoll/core/validators.py` | `inn`, `kpp` (перенесены из `catalog/schemas.py`) | готово |
| `backend/src/quoll/catalog/schemas.py` | берёт валидаторы из `core/validators` | готово |
| `backend/src/quoll/catalog/service.py` | `actor_id: str \| None` у `create`/`update` (актор «система») | готово |
| `backend/src/quoll/core/locking.py` | docstring: `ImportBatch` - первый уровень | готово |
| `backend/src/quoll/auth/audit_models.py` | `TargetType.IMPORT_BATCH`, события `IMPORT_*`, `INTERACTION_IMPORTED` | готово |
| `backend/src/quoll/config.py`, `core/system_defaults.py` | настройки и константы импорта §12.4 | готово |
| `backend/src/quoll/interactions/models.py` | `InteractionDocument.attachment_id` nullable + CHECK | модель готова, код `document_service` ещё не адаптирован (см. ниже) |
| `imports/spec.py` | виды, поля, синонимы, коды IMP, `issue()` | готово (step:* поля реестра - коммит 2) |
| `imports/models.py`, `errors.py` | модели партии, ошибки IMP-0xx | готово |
| `imports/parsing.py` | xlsx/xls/csv/json, строка заголовков, лимит строк | готово |
| `imports/mapping.py` | вид листа, сопоставление колонок, ПДн | готово |
| `imports/normalize.py` | нормализация значений | готово |
| `imports/snapshot.py` | снимок справочников | каталоги готовы; менеджеры, маршрут, заявки вузов - коммит 2 |
| `imports/analysis.py` | разбор строк, ссылки (`{"id"}` / `{"new_of_row","part"}`), части и diff, слияние повторов IMP-140/141, статусы, digest, summary, `analyze(upto=вид)` для перепроверки | каталоги готовы |
| `imports/registry.py` | **заглушка**: строки реестра только разобраны и получают SAME | коммит 2 |
| `imports/apply.py` | применение строки каталога через `catalog_service`, специальности вуза `ON CONFLICT DO NOTHING` | готово |
| `imports/worker.py` | `claim` (SKIP LOCKED, аренда), `run` по видам с перечитанным снимком, `_unit` c savepoint и итогом в транзакции единицы, `_attempt` (попытки, IMP-191), `finish` (общий с `stop`), итоги запросом | каталоги готовы; Keycloak-проверка и единица-группа реестра - коммит 2 |
| `imports/service.py` | upload (лимит 413, ПДн вырезаются), get/list, patch batch (маршрут, `replace_stages` IMP-014), patch sheet (IMP-012/013), rows с фильтрами, patch row (edits, excluded), apply (version, коммит пересчёта перед IMP-018, IMP-011, IMP-019), stop, cancel/drop/cleanup, файлы строк (IMP-015/016/020) | готово без реестровых частей |
| `imports/schemas.py`, `router.py` | `/api/v1/imports` по §21 без `decisions` и `managers` | готово частично |
| `imports/template.py` | `GET /kinds`, шаблон xlsx с «Инструкцией», генератор `docs/import-format.md` | код готов, `docs/import-format.md` ещё не сгенерирован |
| `imports/report.py` | итог партии xlsx | готово |
| `jobs.py`, `main.py` | `Periodic("import-apply")`, `Periodic("import-cleanup")`, подключён роутер | готово |

Проверено: ruff чист; приложение импортируется; локальный тест `backend/tests/test_import_catalogs.py` (каталог `tests` в `.gitignore`, данные `tests/data/seed.xlsx`) проходит - seed: 326 строк NEW, применение через воркер - все APPLIED, повторная загрузка - все SAME (M5).

## Что доделать (коммит 1)

1. `document_service` для документа «Договор» без файла (§19.8, §12.3): `DocumentView.attachment` Optional, outerjoin при чтении, `delete_document` без вложения (не трогать S3), `interactions/router._document` и `DocumentRead.attachment` Optional; новая версия со сканом через `replaces_document_id` (§25 п. 15).
2. Сгенерировать `docs/import-format.md`: `cd backend && uv run python -m quoll.imports.template docs`.
3. Коды §22 в `errors-ru.md` - файла в репозитории нет (он у заказчика): оформить отдельным документом и сообщить пользователю.
4. Тесты §26 п. 1-4, 7, 8 (без реестра), 9 (каталоги), 10, 12 (10 000 строк ≤ 2 с - не мерялось), 13. Сейчас есть только smoke-тест seed.
5. Сверить открытые места:
   - `analysis._sheet_issues`: IMP-017 для листа реестра без опубликованного маршрута - добавить вместе с реестром;
   - ссылка на ненайденную запись (не вуз) даёт IMP-110 - в доке отдельного кода нет;
   - в параметрах IMP-140/141 `ref_row` - номер строки файла;
   - строка примера в шаблоне xlsx стоит по доку, но при забытом удалении загрузится как данные - вынести вопрос пользователю;
   - строки пропущенных листов получают статус EXCLUDED.
6. Прогнать миграции заново (`alembic upgrade head`, `downgrade -1`, `upgrade head`, `check`), ruff, все тесты; коммит `add import batches with parsing and catalogs` уже частично сделан этим WIP-коммитом.

## Что доделать (коммит 2, §27)

Реестр целиком: `registry.py` (§16.6-16.10: группы, сведение IMP-150, программа IMP-151/152, В14 IMP-153, шаг и таблица §16.7, доп. поля §16.8, менеджер §16.9, сравнение с базой и отпечаток REPLACE §16.10), снимок менеджеров и маршрута, `place.py` (§19), `*_locked`-функции сервисов и актор «система» (§19.11), событие `IMPORT` и отметки В27 (`payload.passed`, `payload.sealed`, правка `reached_since_no_return`), В28, `IMPORT_ASSIGNED(_TEAM)`, отчёты (§20 п. 11), защита шаблона ребра от файла партии (`workflows/router`, `is_import_file`), `POST /decisions`, `GET /managers`, поля `decision`/`manager_choice`/`contacts_target` в `RowPatch`, Keycloak в `worker.claim` (§17.2), удаление старого импорта (`interactions/import_service.py`, `POST /interactions/import`, схемы `InteractionImport*`, их тесты). Тесты §26 п. 5, 6, 9 (реестр), 11, 14-23.

## Коммит 3

README (раздел импорта), список отклонений от дока, отчёт пользователю. Мердж в `dev` - только с согласия пользователя.

## Правила пользователя

Вопросы - пользователю, критичное не решать самому. Коммиты - одна короткая строка по-английски в повелительном наклонении, без тела. Комментарии в коде - по-русски, коротко, тире «-», без «ёлочек»; сообщения исключений - по-английски. KISS/DRY, в чужие модули - только по необходимости. Тесты - в `backend/tests` (только локально). Postgres локально: `service postgresql start`.
