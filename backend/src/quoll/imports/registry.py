"""Группы реестра: сведение, шаг, конфликты, доп. поля (import-design §16.6-16.10)"""

from sqlalchemy.ext.asyncio import AsyncSession


async def run(session: AsyncSession, an, rows: list) -> None:
    """реестр - следующим коммитом; пока строки реестра только разобраны"""
