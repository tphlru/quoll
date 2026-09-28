from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Response, status

from quoll.auth.dependencies import (
    AdminOnly,
    AdminUser,
    SupervisorUser,
    get_current_user,
    require_roles,
)
from quoll.auth.models import User, UserRole
from quoll.core import SystemDefaults
from quoll.core.exceptions import DomainRuleException
from quoll.workflows import change_requests, step_handlers, workflow_service
from quoll.workflows.dependencies import (
    SessionDep,
    StageRepoDep,
    TransitionRepoDep,
    WorkflowRepoDep,
)
from quoll.workflows.schemas import (
    StageArchiveRequest,
    StageCreate,
    StageHandlerRead,
    StageRead,
    StageUpdate,
    StartStageRequest,
    WorkflowChangeCreate,
    WorkflowChangeDecision,
    WorkflowChangeRead,
    WorkflowCreate,
    WorkflowDetailRead,
    WorkflowRead,
    WorkflowTransitionCreate,
    WorkflowTransitionRead,
    WorkflowTransitionUpdate,
    WorkflowUpdate,
)

workflows_router = APIRouter(
    prefix="/api/v1/workflows",
    tags=["Workflows"],
    dependencies=[Depends(get_current_user)],
)
stages_router = APIRouter(
    prefix="/api/v1/stages",
    tags=["Workflow Stages"],
    dependencies=[Depends(get_current_user)],
)
transitions_router = APIRouter(
    prefix="/api/v1/transitions",
    tags=["Workflow Transitions"],
    dependencies=[Depends(get_current_user)],
)


# Workflows Endpoints


@workflows_router.post(
    "/publish/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Publish workflow",
    dependencies=[AdminOnly],
)
async def publish_workflow(id: int, admin: AdminUser, session: SessionDep):
    await workflow_service.publish(session, id, admin.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@workflows_router.post(
    "/{id}/start-stage",
    response_model=WorkflowTransitionRead,
    summary="Make another stage the start one",
    dependencies=[AdminOnly],
)
async def change_start_stage(
    body: StartStageRequest,
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Workflow ID"),
):
    return await workflow_service.change_start_stage(
        session, id, body.stage_id, admin.id
    )


@workflows_router.post(
    "/",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new workflow",
    dependencies=[AdminOnly],
)
async def create_workflow(
    schema: WorkflowCreate, admin: AdminUser, session: SessionDep
):
    return await workflow_service.create_workflow(session, schema, admin.id)


@workflows_router.get(
    "/",
    response_model=list[WorkflowRead],
    summary="List workflows with pagination",
)
async def list_workflows(
    repo: WorkflowRepoDep,
    limit: int = Query(
        default=SystemDefaults.DEFAULT_PAGE_SIZE,
        ge=1,
        le=SystemDefaults.MAX_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0),
):
    return await repo.get_all(limit=limit, offset=offset)


@workflows_router.get(
    "/{id}",
    response_model=WorkflowDetailRead,
    summary="Get workflow details with stages and transitions",
)
async def get_workflow(
    repo: WorkflowRepoDep,
    id: int = Path(..., ge=1, description="Workflow ID"),
):
    return await repo.get_with_details(id)


@workflows_router.patch(
    "/{id}",
    response_model=WorkflowRead,
    summary="Partially update a workflow",
    dependencies=[AdminOnly],
)
async def update_workflow(
    schema: WorkflowUpdate,
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Workflow ID"),
):
    return await workflow_service.update_workflow(session, id, schema, admin.id)


@workflows_router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow",
    dependencies=[AdminOnly],
)
async def delete_workflow(
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Workflow ID"),
):
    await workflow_service.delete_workflow(session, id, admin.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Stages Endpoints


@stages_router.post(
    "/",
    response_model=StageRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new stage to a workflow",
    dependencies=[AdminOnly],
)
async def create_stage(schema: StageCreate, admin: AdminUser, session: SessionDep):
    return await workflow_service.create_stage(session, schema, admin.id)


@stages_router.get(
    "/by-workflow/{workflow_id}",
    response_model=list[StageRead],
    summary="Get stages for a workflow ordered by position",
)
async def get_stages_by_workflow(
    repo: StageRepoDep,
    workflow_id: int = Path(..., ge=1, description="Workflow ID"),
):
    return await repo.get_by_workflow_id(workflow_id)


@stages_router.get(
    "/handlers",
    response_model=list[StageHandlerRead],
    summary="Special behaviours a side stage can have",
)
async def list_stage_handlers():
    return [
        StageHandlerRead(code=code, label=label)
        for code, label in step_handlers.HANDLERS.items()
    ]


@stages_router.get(
    "/{id}",
    response_model=StageRead,
    summary="Get stage by ID",
)
async def get_stage(
    repo: StageRepoDep,
    id: int = Path(..., ge=1, description="Stage ID"),
):
    return await repo.get(id)


@stages_router.patch(
    "/{id}",
    response_model=StageRead,
    summary="Partially update a stage",
    dependencies=[AdminOnly],
)
async def update_stage(
    schema: StageUpdate,
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Stage ID"),
):
    return await workflow_service.update_stage(session, id, schema, admin.id)


@stages_router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a stage",
    dependencies=[AdminOnly],
)
async def delete_stage(
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Stage ID"),
):
    await workflow_service.delete_stage(session, id, admin.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@stages_router.post(
    "/{id}/archive",
    response_model=StageRead,
    summary="Archive a stage of a live workflow, moving its interactions",
    dependencies=[AdminOnly],
)
async def archive_stage(
    body: StageArchiveRequest,
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Stage ID"),
):
    return await workflow_service.archive_stage(
        session, id, body.relocate_to_stage_id, admin.id
    )


# Transitions Endpoints


@transitions_router.post(
    "/",
    response_model=WorkflowTransitionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a transition between stages",
    dependencies=[AdminOnly],
)
async def create_transition(
    schema: WorkflowTransitionCreate, admin: AdminUser, session: SessionDep
):
    return await workflow_service.create_transition(session, schema, admin.id)


@transitions_router.get(
    "/available",
    response_model=list[WorkflowTransitionRead],
    summary="Get available active transitions from a given stage",
)
async def get_available_transitions(
    repo: TransitionRepoDep,
    workflow_id: int = Query(..., ge=1, description="Workflow ID"),
    from_stage_id: int | None = Query(
        default=None, ge=1, description="Current stage ID (None for initial entry)"
    ),
):
    return await repo.get_available_transitions(
        workflow_id=workflow_id, from_stage_id=from_stage_id
    )


@transitions_router.get(
    "/{id}",
    response_model=WorkflowTransitionRead,
    summary="Get a transition by ID with its attachments",
)
async def get_transition(
    repo: TransitionRepoDep,
    id: int = Path(..., ge=1, description="Transition ID"),
):
    return await repo.get(id)


@transitions_router.patch(
    "/{id}",
    response_model=WorkflowTransitionRead,
    summary="Partially update a transition",
    dependencies=[AdminOnly],
)
async def update_transition(
    schema: WorkflowTransitionUpdate,
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Transition ID"),
):
    return await workflow_service.update_transition(session, id, schema, admin.id)


@transitions_router.post(
    "/{id}/attachments/{attachment_id}",
    status_code=status.HTTP_201_CREATED,
    summary="Link an attachment to a transition",
    dependencies=[AdminOnly],
)
async def link_attachment(
    repo: TransitionRepoDep,
    admin: AdminUser,
    id: int = Path(..., ge=1, description="Transition ID"),
    attachment_id: int = Path(..., ge=1, description="Attachment ID"),
):
    await repo.get(id)
    # файл проекта шаблоном не делается: шаблон читает любой вошедший
    # здесь, а не наверху: модуль заявок сам импортирует модели воркфлоу
    from quoll.interactions.document_service import (
        is_interaction_document,
    )

    if await is_interaction_document(repo.session, attachment_id):
        raise DomainRuleException(409, "Interaction document cannot become a template")
    await repo.link_attachment(id, attachment_id)
    workflow_service.journal_template(
        repo.session, admin.id, id, attachment_id, linked=True
    )
    return Response(status_code=status.HTTP_201_CREATED)


@transitions_router.delete(
    "/{id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink an attachment from a transition",
    dependencies=[AdminOnly],
)
async def unlink_attachment(
    repo: TransitionRepoDep,
    admin: AdminUser,
    id: int = Path(..., ge=1, description="Transition ID"),
    attachment_id: int = Path(..., ge=1, description="Attachment ID"),
):
    await repo.get(id)
    await repo.unlink_attachment(id, attachment_id)
    workflow_service.journal_template(
        repo.session, admin.id, id, attachment_id, linked=False
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@transitions_router.delete(
    "/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a transition",
    dependencies=[AdminOnly],
)
async def delete_transition(
    admin: AdminUser,
    session: SessionDep,
    id: int = Path(..., ge=1, description="Transition ID"),
):
    await workflow_service.delete_transition(session, id, admin.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


change_requests_router = APIRouter(
    prefix="/api/v1/workflow-change-requests",
    tags=["Workflow change requests"],
    dependencies=[Depends(get_current_user)],
)


@change_requests_router.post(
    "/", response_model=WorkflowChangeRead, status_code=status.HTTP_201_CREATED
)
async def request_workflow_change(
    body: WorkflowChangeCreate, user: SupervisorUser, session: SessionDep
):
    return await change_requests.create(
        session, workflow_id=body.workflow_id, text=body.text, actor_id=user.id
    )


@change_requests_router.get("/", response_model=list[WorkflowChangeRead])
async def list_workflow_changes(
    user: Annotated[User, Depends(require_roles(UserRole.SUPERVISER, UserRole.ADMIN))],
    session: SessionDep,
    status_filter: Annotated[
        Literal["PENDING", "APPROVED", "REJECTED"] | None, Query(alias="status")
    ] = None,
    limit: int = Query(
        default=SystemDefaults.DEFAULT_PAGE_SIZE, ge=1, le=SystemDefaults.MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
):
    return await change_requests.visible(session, user, status_filter, limit, offset)


@change_requests_router.post("/{id}/approve", response_model=WorkflowChangeRead)
async def approve_workflow_change(
    id: int, body: WorkflowChangeDecision, admin: AdminUser, session: SessionDep
):
    return await change_requests.decide(
        session, request_id=id, actor_id=admin.id, approve=True, comment=body.comment
    )


@change_requests_router.post("/{id}/reject", response_model=WorkflowChangeRead)
async def reject_workflow_change(
    id: int, body: WorkflowChangeDecision, admin: AdminUser, session: SessionDep
):
    return await change_requests.decide(
        session, request_id=id, actor_id=admin.id, approve=False, comment=body.comment
    )
