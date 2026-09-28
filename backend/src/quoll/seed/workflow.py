"""Эталонный воркфлоу «Работа с вузами» (Д17).

собирается теми же сервисами, что и админские ручки: всё, что делает сид,
админ повторяет руками через API. Повторный запуск ничего не делает -
воркфлоу с таким именем уже есть. Всё одной транзакцией вызывающего:
сбой посередине не оставит полусобранный черновик
"""

import json
from importlib import resources

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quoll.workflows import workflow_service
from quoll.workflows.models import Workflow
from quoll.workflows.schemas import (
    StageCreate,
    WorkflowCreate,
    WorkflowTransitionCreate,
)

SOURCE = "workflow_universities.json"


def spec() -> dict:
    return json.loads(resources.files("quoll.seed").joinpath(SOURCE).read_text())


async def ensure_reference_workflow(session: AsyncSession) -> Workflow | None:
    """None - уже есть"""
    data = spec()
    if await session.scalar(select(Workflow.id).where(Workflow.name == data["name"])):
        return None
    workflow = await workflow_service.create_workflow(
        session,
        WorkflowCreate(
            name=data["name"],
            description=data["description"],
            warn_days=data["warn_days"],
        ),
        None,
    )
    ids: dict[str, int] = {}
    for stage in data["stages"]:
        created = await workflow_service.create_stage(
            session,
            StageCreate(
                workflow_id=workflow.id,
                name=stage["name"],
                position=stage["position"],
                is_terminal=stage.get("terminal", False),
                is_parallel=stage.get("parallel", False),
                is_branch_stage=stage.get("branch", False),
                is_branch_start=stage.get("branch_start", False),
                parent_stage_id=ids.get(stage.get("parent")),
                stall_days=stage.get("stall_days"),
                passive_after_days=stage.get("passive_after_days"),
                fields=stage.get("fields", []),
            ),
            None,
        )
        ids[stage["key"]] = created.id
    for edge in data["transitions"]:
        await workflow_service.create_transition(
            session,
            WorkflowTransitionCreate(
                workflow_id=workflow.id,
                name=edge["name"],
                from_stage_id=ids.get(edge.get("from")),
                to_stage_id=ids[edge["to"]],
                requires_approval=edge.get("requires_approval", False),
                reject_to_stage_id=ids.get(edge.get("reject_to")),
                is_backward=edge.get("backward", False),
                is_irreversible=edge.get("irreversible", False),
                required_document_kinds=edge.get("required_document_kinds", []),
            ),
            None,
        )
    await workflow_service.publish(session, workflow.id, None)
    return workflow
