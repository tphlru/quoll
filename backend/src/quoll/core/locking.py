"""Блокировки строк в едином порядке.

Порядок: ImportBatch -> Superviser -> Admin -> Manager -> User -> Interaction ->
InteractionRequest -> PendingOrgAction -> Workflow -> Stage ->
WorkflowTransition, внутри уровня по возрастанию id. Иначе дедлок.

партия импорта - первой: единица применения держит её до конца своей
транзакции и уже под ней берёт стадии и область заявки

стадия раньше ребра: переход берёт целевую стадию, потом ребро, а архивация
стадии деактивирует её рёбра последними. Архивация переносит заявки уже после
своей стадии - цикла нет, пока петли из стадии в неё же запрещены CHECK-ом

ветки продуктов своих блокировок не имеют - их прикрывает взаимодействие.
Но с ветками взаимодействие стоит на нескольких стадиях, и архивация одной
из них берёт его после стадии. Поэтому ход ветки (и решение по её аппруву)
берёт FOR SHARE на целевую стадию раньше взаимодействия - share_stage

select(Manager).with_for_update() не годится - Manager наследует User, и запрос
блокирует ещё и строку users, причём раньше. Поэтому везде FOR ... OF.

Режим FOR NO KEY UPDATE, а не FOR UPDATE: ключи у нас не меняются, а FOR UPDATE
конфликтует с FOR KEY SHARE, который берёт проверка внешнего ключа. С ним
вставка строки со ссылкой на заблокированную ждала бы - и ловила дедлоки
"""

import logging
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.db import Base

logger = logging.getLogger(__name__)


def lock_stmt[ModelType: Base](model: type[ModelType], ident: Any) -> Select:
    return (
        select(model)
        .where(model.id == ident)  # pyright: ignore [reportAttributeAccessIssue]
        .with_for_update(of=model.__table__, key_share=True)
        # объект мог попасть в сессию раньше, до блокировки - без этого
        # SQLAlchemy отдал бы его как есть, со старыми значениями
        .execution_options(populate_existing=True)
    )


async def lock_row[ModelType: Base](
    session: AsyncSession, model: type[ModelType], ident: Any
) -> ModelType | None:
    """заблокировать одну строку нужного уровня"""
    logger.debug(f"Locking {model.__name__} id={ident}")
    return (await session.execute(lock_stmt(model, ident))).scalar_one_or_none()


async def lock_rows[ModelType: Base](
    session: AsyncSession, model: type[ModelType], idents: list[Any]
) -> dict[Any, ModelType]:
    """заблокировать несколько строк одного уровня по возрастанию id.

    по одному запросу на строку - иначе порядок захвата решает планировщик
    """
    locked: dict[Any, ModelType] = {}
    for ident in sorted({i for i in idents if i is not None}):
        row = await lock_row(session, model, ident)
        if row is not None:
            locked[ident] = row
    return locked
