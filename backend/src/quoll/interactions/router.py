import json
from datetime import date
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from quoll.attachments.dependencies import AttachmentServiceDep
from quoll.attachments.schemas import AttachmentRead
from quoll.auth.dependencies import (
    AdminOnly,
    AdminUser,
    CurrentUser,
    ManagerUser,
    SupervisorUser,
    get_current_user,
)
from quoll.auth.models import UserRole
from quoll.core import SystemDefaults
from quoll.core.exceptions import DomainRuleException
from quoll.interactions import (
    branch_service,
    contract_service,
    document_service,
    import_service,
    project_service,
    request_service,
    sa_service,
    side_pointer_service,
    slots,
    step_service,
    transition_service,
)
from quoll.interactions.access_policy import readable_filter
from quoll.interactions.dependencies import (
    ChangeableInteraction,
    InteractionId,
    InteractionRepoDep,
    ReadableInteraction,
    SessionDep,
)
from quoll.interactions.models import InteractionStatus, RequestKind, RequestStatus
from quoll.interactions.schemas import (
    AcceptRequest,
    AgreementActionRead,
    AgreementActionWrite,
    AgreementRead,
    AgreementUpdate,
    AssignRequest,
    BranchRead,
    BranchWrite,
    CloseRequest,
    CommentRequest,
    ContractStatusWrite,
    DocumentDecision,
    DocumentRead,
    InteractionCreate,
    InteractionDetailRead,
    InteractionHistoryRead,
    InteractionImportResult,
    InteractionRead,
    InteractionSettings,
    InteractionUpdate,
    PauseRequest,
    ReasonedRequest,
    ReopenRequest,
    RequestApprove,
    RequestCreate,
    RequestRead,
    RequestReject,
    RollbackRequest,
    SidePointerCancel,
    SidePointerRead,
    SidePointerStart,
    SidePointerTransition,
    StageValuesRead,
    StageValuesWrite,
    TransitionRequest,
)
from quoll.notifications import queries as notification_queries
from quoll.notifications.schemas import NotificationHistoryRead

interactions_router = APIRouter(
    prefix="/api/v1/interactions",
    tags=["Interactions"],
    dependencies=[Depends(get_current_user)],
)


# Interactions Endpoints


@interactions_router.post(
    "/import",
    response_model=InteractionImportResult,
    summary="Import interactions from an xls/xlsx registry, each offered to its manager",
)
async def import_interactions(
    admin: AdminUser,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    workflow_id: Annotated[int, Form()],
    dry_run: bool = False,
):
    try:
        rows = import_service.read_rows(await file.read(), file.filename)
    except Exception as e:
        raise HTTPException(400, detail=f"Invalid excel file: {e}") from e
    return await import_service.import_rows(
        session, rows, workflow_id=workflow_id, actor_id=admin.id, dry_run=dry_run
    )


@interactions_router.post(
    "/",
    response_model=InteractionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft interaction, visible to its author until assigned",
)
async def create_interaction(
    schema: InteractionCreate, user: SupervisorUser, session: SessionDep
):
    return await project_service.create_interaction(session, schema, user.id)


@interactions_router.get(
    "/",
    response_model=list[InteractionRead],
    summary="List interactions visible to the current user",
)
async def list_interactions(
    user: CurrentUser,
    repo: InteractionRepoDep,
    university_id: int | None = Query(
        default=None, ge=1, description="Filter by University ID"
    ),
    program_id: int | None = Query(
        default=None, ge=1, description="Filter by IT program of any branch"
    ),
    status: InteractionStatus | None = None,
    limit: int = Query(
        default=SystemDefaults.DEFAULT_PAGE_SIZE,
        ge=1,
        le=SystemDefaults.MAX_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0),
):
    return await repo.list_visible(
        readable_filter(user),
        university_id=university_id,
        program_id=program_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@interactions_router.post(
    "/{id}/assign",
    response_model=InteractionRead,
    summary="Assign or reassign an interaction to a manager",
)
async def assign_interaction(
    id: InteractionId,
    body: AssignRequest,
    user: SupervisorUser,
    session: SessionDep,
):
    return await project_service.assign(
        session,
        interaction_id=id,
        actor_id=user.id,
        manager_id=body.manager_id,
        expected_owner_id=body.expected_owner_id,
        reason=body.reason,
    )


@interactions_router.post(
    "/{id}/transition",
    response_model=InteractionRead,
    summary="Move an interaction along an active workflow edge",
)
async def move_interaction(
    id: InteractionId,
    body: TransitionRequest,
    user: CurrentUser,
    session: SessionDep,
):
    return await transition_service.transition(
        session,
        interaction_id=id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        expected_state_id=body.expected_state_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/accept",
    response_model=InteractionRead,
    summary="Owner accepts an assigned interaction and puts it on the start stage",
)
async def accept_interaction(
    id: InteractionId, body: AcceptRequest, user: CurrentUser, session: SessionDep
):
    return await transition_service.accept(
        session,
        interaction_id=id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/rollback",
    response_model=InteractionRead,
    summary="Owner's supervisor returns the interaction several steps back",
)
async def rollback_interaction(
    id: InteractionId, body: RollbackRequest, user: CurrentUser, session: SessionDep
):
    return await transition_service.rollback(
        session,
        interaction_id=id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        expected_state_id=body.expected_state_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/decline",
    response_model=InteractionRead,
    summary="Owner declines an interaction not accepted yet, it returns to the author",
)
async def decline_interaction(
    id: InteractionId, body: CommentRequest, user: CurrentUser, session: SessionDep
):
    return await project_service.decline(
        session, interaction_id=id, actor_id=user.id, comment=body.comment
    )


@interactions_router.post(
    "/{id}/close",
    response_model=InteractionRead,
    summary="Close an interaction early into a terminal stage",
)
async def close_interaction(
    id: InteractionId,
    body: CloseRequest,
    user: CurrentUser,
    session: SessionDep,
):
    return await transition_service.close(
        session,
        interaction_id=id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        expected_state_id=body.expected_state_id,
        close_reason_id=body.close_reason_id,
        branch_close_reason_id=body.branch_close_reason_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/reopen",
    response_model=InteractionRead,
    summary="Return a closed interaction to work with a chosen manager and stage",
)
async def reopen_interaction(
    id: InteractionId,
    body: ReopenRequest,
    user: CurrentUser,
    session: SessionDep,
):
    return await project_service.reopen(
        session,
        interaction_id=id,
        actor_id=user.id,
        manager_id=body.manager_id,
        to_stage_id=body.to_stage_id,
        expected_owner_id=body.expected_owner_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/requests",
    response_model=RequestRead,
    status_code=status.HTTP_201_CREATED,
    summary="Ask the supervisor to transfer or close the interaction",
)
async def create_request(
    id: InteractionId,
    body: RequestCreate,
    user: ManagerUser,
    session: SessionDep,
):
    return await request_service.create(
        session,
        interaction_id=id,
        actor_id=user.id,
        kind=RequestKind(body.kind),
        target_stage_id=body.target_stage_id,
        target_manager_id=body.target_manager_id,
        reason=body.reason,
        branch_id=body.branch_id,
        close_reason_id=body.close_reason_id,
        branch_close_reason_id=body.branch_close_reason_id,
        side_pointer_id=body.side_pointer_id,
    )


def _json_object(raw: str | None) -> dict:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as err:
        raise DomainRuleException(422, "metadata must be a JSON object") from err
    if not isinstance(value, dict):
        raise DomainRuleException(422, "metadata must be a JSON object")
    return value


def _document(view) -> DocumentRead:
    doc = view.document
    return DocumentRead(
        id=doc.id,
        interaction_id=doc.interaction_id,
        stage_id=doc.stage_id,
        uploaded_by=doc.uploaded_by,
        branch_id=doc.branch_id,
        replaces_document_id=doc.replaces_document_id,
        title=doc.title,
        kind=doc.kind,
        description=doc.description,
        contract_number=doc.contract_number,
        contract_signed_at=doc.contract_signed_at,
        contract_valid_until=doc.contract_valid_until,
        supplementary_agreement_id=doc.supplementary_agreement_id,
        side_pointer_id=doc.side_pointer_id,
        metadata=doc.meta,
        is_current=view.is_current,
        replaced_by_id=view.replaced_by.id if view.replaced_by else None,
        replaced_on_stage_id=view.replaced_by.stage_id if view.replaced_by else None,
        status=doc.status,
        created_at=doc.created_at,
        attachment=AttachmentRead.model_validate(view.attachment),
    )


@interactions_router.get("/{id}/documents", response_model=list[DocumentRead])
async def list_documents(interaction: ReadableInteraction, session: SessionDep):
    return [
        _document(v) for v in await document_service.documents(session, interaction.id)
    ]


@interactions_router.post(
    "/{id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a project file to a chosen stage, optionally as a new version",
)
async def attach_document(
    id: InteractionId,
    user: CurrentUser,
    session: SessionDep,
    attachments: AttachmentServiceDep,
    file: Annotated[UploadFile, File()],
    # стадия - снаружи: приложить можно к любой стадии воркфлоу
    stage_id: Annotated[int, Form()],
    replaces_document_id: Annotated[int | None, Form()] = None,
    # файл шага ветки продукта
    branch_id: Annotated[int | None, Form()] = None,
    # файл доп. прохождения
    side_pointer_id: Annotated[int | None, Form()] = None,
    title: Annotated[str | None, Form(max_length=255)] = None,
    # вид из справочника; у новой версии можно не указывать
    kind: Annotated[str | None, Form(max_length=50)] = None,
    description: Annotated[str | None, Form()] = None,
    contract_number: Annotated[str | None, Form(max_length=100)] = None,
    contract_signed_at: Annotated[date | None, Form()] = None,
    contract_valid_until: Annotated[date | None, Form()] = None,
    # multipart не несёт вложенных объектов - JSON строкой
    metadata: Annotated[str | None, Form()] = None,
):
    view = await document_service.upload(
        session,
        attachments,
        interaction_id=id,
        actor=user,
        file=file,
        stage_id=stage_id,
        replaces_document_id=replaces_document_id,
        branch_id=branch_id,
        side_pointer_id=side_pointer_id,
        fields=document_service.DocumentFields(
            kind=kind,
            title=title,
            description=description,
            contract_number=contract_number,
            contract_signed_at=contract_signed_at,
            contract_valid_until=contract_valid_until,
            meta=_json_object(metadata),
        ),
    )
    return _document(view)


@interactions_router.post(
    "/{id}/activate",
    response_model=InteractionRead,
    summary="The manager returns a passive interaction into active slots",
)
async def activate_interaction(
    id: InteractionId, user: CurrentUser, session: SessionDep
):
    return await slots.activate(session, interaction_id=id, actor_id=user.id)


@interactions_router.post(
    "/{id}/passivate",
    response_model=InteractionRead,
    summary="The manager moves a signed interaction on long-term steps to passive",
)
async def passivate_interaction(
    id: InteractionId, user: CurrentUser, session: SessionDep
):
    return await slots.passivate(session, interaction_id=id, actor_id=user.id)


@interactions_router.patch(
    "/{id}/settings",
    response_model=InteractionRead,
    summary="Owner's supervisor sets stall thresholds per step and warning terms",
)
async def update_interaction_settings(
    id: InteractionId, body: InteractionSettings, user: CurrentUser, session: SessionDep
):
    return await project_service.update_settings(
        session,
        interaction_id=id,
        actor_id=user.id,
        changes=body.model_dump(exclude_unset=True),
    )


@interactions_router.post(
    "/{id}/pause",
    response_model=InteractionRead,
    summary="Pause an interaction or replace its pause",
)
async def pause_interaction(
    id: InteractionId, body: PauseRequest, user: CurrentUser, session: SessionDep
):
    return await project_service.pause(
        session,
        interaction_id=id,
        actor_id=user.id,
        until=body.until,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/unpause",
    response_model=InteractionRead,
    summary="Resume a paused interaction",
)
async def unpause_interaction(
    id: InteractionId, user: CurrentUser, session: SessionDep
):
    return await project_service.unpause(session, interaction_id=id, actor_id=user.id)


@interactions_router.get(
    "/{id}",
    response_model=InteractionDetailRead,
    summary="Get interaction details with fully hydrated relations",
)
async def get_interaction(interaction: ReadableInteraction):
    return interaction


@interactions_router.patch(
    "/{id}",
    response_model=InteractionRead,
    summary="Update descriptive fields of an interaction",
)
async def update_interaction(
    schema: InteractionUpdate,
    interaction: ChangeableInteraction,
    user: CurrentUser,
    session: SessionDep,
):
    return await project_service.update_fields(session, interaction, schema, user.id)


@interactions_router.get(
    "/{id}/notifications",
    response_model=list[NotificationHistoryRead],
    summary="What was notified about an interaction, to whom and whether read",
)
async def interaction_notifications(
    interaction: ReadableInteraction,
    user: CurrentUser,
    repo: InteractionRepoDep,
    session: SessionDep,
):
    """руководитель владельца и админ видят все доставки, остальные - свои"""
    ownership = await repo.ownership(interaction)
    sees_all = user.role == UserRole.ADMIN or ownership.owner_superviser_id == user.id
    return await notification_queries.history(
        session, interaction_id=interaction.id, only_user=None if sees_all else user.id
    )


SA = "/{id}/supplementary-agreements"


@interactions_router.get(SA, response_model=list[AgreementRead])
async def list_agreements(interaction: ReadableInteraction, session: SessionDep):
    return await sa_service.listing(session, interaction.id)


@interactions_router.patch(SA + "/{sa_id}", response_model=AgreementRead)
async def update_agreement(
    id: InteractionId,
    sa_id: int,
    body: AgreementUpdate,
    user: CurrentUser,
    session: SessionDep,
):
    sa = await sa_service.update(
        session,
        interaction_id=id,
        sa_id=sa_id,
        actor_id=user.id,
        changes=body.model_dump(exclude_unset=True),
    )
    return await sa_service.view(session, sa)


@interactions_router.post(
    SA + "/{sa_id}/actions",
    response_model=AgreementActionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_agreement_action(
    id: InteractionId,
    sa_id: int,
    body: AgreementActionWrite,
    user: CurrentUser,
    session: SessionDep,
):
    return await sa_service.add_action(
        session,
        interaction_id=id,
        sa_id=sa_id,
        actor_id=user.id,
        fields=body.model_dump(exclude_none=True),
    )


@interactions_router.delete(
    SA + "/{sa_id}/actions/{action_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_agreement_action(
    id: InteractionId,
    sa_id: int,
    action_id: int,
    user: CurrentUser,
    session: SessionDep,
):
    await sa_service.remove_action(
        session, interaction_id=id, sa_id=sa_id, action_id=action_id, actor_id=user.id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@interactions_router.post(
    SA + "/{sa_id}/scan",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Scan of the agreement - its own button, the kind is set automatically",
)
async def upload_agreement_scan(
    id: InteractionId,
    sa_id: int,
    user: CurrentUser,
    session: SessionDep,
    attachments: AttachmentServiceDep,
    file: Annotated[UploadFile, File()],
    replaces_document_id: Annotated[int | None, Form()] = None,
):
    view = await sa_service.upload_scan(
        session,
        attachments,
        interaction_id=id,
        sa_id=sa_id,
        actor=user,
        file=file,
        replaces_document_id=replaces_document_id,
    )
    return _document(view)


SP = "/{id}/side-pointers"


@interactions_router.post(
    SP,
    response_model=SidePointerRead,
    status_code=status.HTTP_201_CREATED,
    summary="Send a side pointer through side steps the main route has passed",
)
async def start_side_pointer(
    id: InteractionId, body: SidePointerStart, user: CurrentUser, session: SessionDep
):
    return await side_pointer_service.start(
        session,
        interaction_id=id,
        actor_id=user.id,
        stage_id=body.stage_id,
        comment=body.comment,
    )


@interactions_router.get(SP, response_model=list[SidePointerRead])
async def list_side_pointers(interaction: ReadableInteraction, session: SessionDep):
    return await side_pointer_service.listing(session, interaction.id)


@interactions_router.post(
    SP + "/{pointer_id}/transition",
    response_model=SidePointerRead,
    summary="Move a side pointer; a forward edge to the main route finishes it",
)
async def move_side_pointer(
    id: InteractionId,
    pointer_id: int,
    body: SidePointerTransition,
    user: CurrentUser,
    session: SessionDep,
):
    return await side_pointer_service.move(
        session,
        interaction_id=id,
        pointer_id=pointer_id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        expected_state_id=body.expected_state_id,
        comment=body.comment,
    )


@interactions_router.post(
    SP + "/{pointer_id}/cancel",
    response_model=SidePointerRead,
    summary="Cancel a side pointer; its open agreement and requests are cancelled",
)
async def cancel_side_pointer(
    id: InteractionId,
    pointer_id: int,
    body: SidePointerCancel,
    user: CurrentUser,
    session: SessionDep,
):
    return await side_pointer_service.cancel(
        session,
        interaction_id=id,
        pointer_id=pointer_id,
        actor_id=user.id,
        comment=body.comment,
    )


@interactions_router.get(
    "/{id}/history",
    response_model=InteractionHistoryRead,
    summary="Stage moves and assignments of an interaction",
)
async def interaction_history(
    interaction: ReadableInteraction, repo: InteractionRepoDep
) -> InteractionHistoryRead:
    return InteractionHistoryRead(
        stages=await repo.stage_history(interaction.id),
        assignments=await repo.assignments(interaction.id),
    )


@interactions_router.get(
    "/{id}/stage-values",
    response_model=list[StageValuesRead],
    summary="Filled fields of every stage of an interaction",
)
async def list_stage_values(interaction: ReadableInteraction, session: SessionDep):
    return await step_service.all_values(session, interaction.id)


@interactions_router.put(
    "/{id}/stage-values/{stage_id}",
    response_model=StageValuesRead,
    summary="Replace field values of the current or a passed stage",
)
async def put_stage_values(
    id: InteractionId,
    stage_id: int,
    body: StageValuesWrite,
    user: CurrentUser,
    session: SessionDep,
    branch_id: int | None = None,
    side_pointer_id: int | None = None,
):
    row = await step_service.set_values(
        session,
        interaction_id=id,
        stage_id=stage_id,
        values=body.values,
        actor_id=user.id,
        branch_id=branch_id,
        side_pointer_id=side_pointer_id,
    )
    return await step_service.view(session, row)


@interactions_router.post(
    "/{id}/stage-values/{stage_id}/approve",
    response_model=StageValuesRead,
    summary="Supervisor applies a pending edit of a passed step",
)
async def approve_stage_values(
    id: InteractionId,
    stage_id: int,
    user: CurrentUser,
    session: SessionDep,
    branch_id: int | None = None,
    side_pointer_id: int | None = None,
):
    row = await step_service.decide(
        session,
        interaction_id=id,
        stage_id=stage_id,
        actor_id=user.id,
        approve=True,
        branch_id=branch_id,
        side_pointer_id=side_pointer_id,
    )
    return await step_service.view(session, row)


@interactions_router.post(
    "/{id}/stage-values/{stage_id}/reject",
    response_model=StageValuesRead,
    summary="Supervisor drops a pending edit of a passed step",
)
async def reject_stage_values(
    id: InteractionId,
    stage_id: int,
    user: CurrentUser,
    session: SessionDep,
    branch_id: int | None = None,
    side_pointer_id: int | None = None,
):
    row = await step_service.decide(
        session,
        interaction_id=id,
        stage_id=stage_id,
        actor_id=user.id,
        approve=False,
        branch_id=branch_id,
        side_pointer_id=side_pointer_id,
    )
    return await step_service.view(session, row)


@interactions_router.post(
    "/{id}/branches",
    response_model=BranchRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a program, optionally with one of its products, before signing",
)
async def add_branch(
    id: InteractionId, body: BranchWrite, user: CurrentUser, session: SessionDep
):
    return await contract_service.add_branch(
        session,
        interaction_id=id,
        program_id=body.program_id,
        product_id=body.product_id,
        actor_id=user.id,
    )


@interactions_router.patch(
    "/{id}/branches/{branch_id}",
    response_model=BranchRead,
    summary="Mark a draft branch proposed, approved or rejected by the university",
)
async def set_branch_contract_status(
    id: InteractionId,
    branch_id: int,
    body: ContractStatusWrite,
    user: CurrentUser,
    session: SessionDep,
):
    return await contract_service.set_status(
        session,
        interaction_id=id,
        branch_id=branch_id,
        status=body.contract_status,
        actor_id=user.id,
    )


@interactions_router.delete(
    "/{id}/branches/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Drop a draft branch, before signing",
)
async def remove_branch(
    id: InteractionId, branch_id: int, user: CurrentUser, session: SessionDep
):
    await contract_service.remove_branch(
        session, interaction_id=id, branch_id=branch_id, actor_id=user.id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@interactions_router.post(
    "/{id}/branches/{branch_id}/transition",
    response_model=BranchRead,
    summary="Move a product branch along an active edge",
)
async def move_branch(
    id: InteractionId,
    branch_id: int,
    body: TransitionRequest,
    user: CurrentUser,
    session: SessionDep,
):
    if body.expected_state_id is None:
        raise DomainRuleException(422, "Branch always stands on a stage")
    return await branch_service.move(
        session,
        interaction_id=id,
        branch_id=branch_id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        expected_state_id=body.expected_state_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/branches/{branch_id}/rollback",
    response_model=BranchRead,
    summary="Owner's supervisor returns a product branch several steps back",
)
async def rollback_branch(
    id: InteractionId,
    branch_id: int,
    body: RollbackRequest,
    user: CurrentUser,
    session: SessionDep,
):
    return await branch_service.rollback(
        session,
        interaction_id=id,
        branch_id=branch_id,
        actor_id=user.id,
        to_stage_id=body.to_stage_id,
        expected_state_id=body.expected_state_id,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/branches/{branch_id}/pause",
    response_model=BranchRead,
    summary="Pause one branch; the manager's slot is not affected",
)
async def pause_branch(
    id: InteractionId,
    branch_id: int,
    body: PauseRequest,
    user: CurrentUser,
    session: SessionDep,
):
    return await branch_service.pause(
        session,
        interaction_id=id,
        branch_id=branch_id,
        actor_id=user.id,
        until=body.until,
        comment=body.comment,
    )


@interactions_router.post(
    "/{id}/branches/{branch_id}/unpause",
    response_model=BranchRead,
    summary="Resume a paused branch",
)
async def unpause_branch(
    id: InteractionId, branch_id: int, user: CurrentUser, session: SessionDep
):
    return await branch_service.unpause(
        session, interaction_id=id, branch_id=branch_id, actor_id=user.id
    )


@interactions_router.post(
    "/{id}/branches/{branch_id}/close",
    response_model=BranchRead,
    summary="Owner's supervisor closes a branch early, it keeps its step",
)
async def close_branch(
    id: InteractionId,
    branch_id: int,
    body: ReasonedRequest,
    user: CurrentUser,
    session: SessionDep,
):
    return await branch_service.close(
        session,
        interaction_id=id,
        branch_id=branch_id,
        actor_id=user.id,
        close_reason_id=body.close_reason_id,
        comment=body.comment,
    )


@interactions_router.get(
    "/{id}/branches",
    response_model=list[BranchRead],
    summary="Branches: drafts before signing, then each on its own steps",
)
async def list_branches(interaction: ReadableInteraction, session: SessionDep):
    return await contract_service.branches(session, interaction.id)


@interactions_router.post(
    "/{id}/cancel",
    response_model=InteractionRead,
    summary="Cancel a draft that never entered a stage - nothing is deleted",
)
async def cancel_interaction(
    id: InteractionId, body: ReasonedRequest, user: CurrentUser, session: SessionDep
):
    return await project_service.cancel_draft(
        session,
        interaction_id=id,
        actor_id=user.id,
        close_reason_id=body.close_reason_id,
        comment=body.comment,
    )


requests_router = APIRouter(
    prefix="/api/v1/requests",
    tags=["Interaction requests"],
    dependencies=[Depends(get_current_user)],
)


@requests_router.get("/", response_model=list[RequestRead])
async def list_requests(
    user: CurrentUser,
    session: SessionDep,
    status_filter: Annotated[RequestStatus | None, Query(alias="status")] = None,
    limit: int = Query(
        default=SystemDefaults.DEFAULT_PAGE_SIZE, ge=1, le=SystemDefaults.MAX_PAGE_SIZE
    ),
    offset: int = Query(default=0, ge=0),
):
    return await request_service.visible(session, user, status_filter, limit, offset)


@requests_router.post("/{id}/approve", response_model=RequestRead)
async def approve_request(
    id: int, body: RequestApprove, user: SupervisorUser, session: SessionDep
):
    decision = await request_service.approve(
        session,
        request_id=id,
        actor_id=user.id,
        target_manager_id=body.target_manager_id,
        comment=body.comment,
    )
    if decision.refused is not None:
        # без исключения: сессия закоммитит отмену устаревшей просьбы
        return JSONResponse(status_code=409, content={"detail": decision.refused})
    return decision.request


@requests_router.post("/{id}/reject", response_model=RequestRead)
async def reject_request(
    id: int, body: RequestReject, user: SupervisorUser, session: SessionDep
):
    return await request_service.reject(
        session, request_id=id, actor_id=user.id, comment=body.comment
    )


@requests_router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_request(id: int, user: ManagerUser, session: SessionDep):
    await request_service.withdraw(session, request_id=id, actor_id=user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


documents_router = APIRouter(
    prefix="/api/v1/documents",
    tags=["Interaction documents"],
    dependencies=[Depends(get_current_user)],
)


@documents_router.post("/{id}/approve", response_model=DocumentRead)
async def approve_document(
    id: int, body: DocumentDecision, user: CurrentUser, session: SessionDep
):
    return _document(
        await document_service.decide(
            session,
            document_id=id,
            actor_id=user.id,
            approve=True,
            comment=body.comment,
        )
    )


@documents_router.post("/{id}/reject", response_model=DocumentRead)
async def reject_document(
    id: int, body: DocumentDecision, user: CurrentUser, session: SessionDep
):
    return _document(
        await document_service.decide(
            session,
            document_id=id,
            actor_id=user.id,
            approve=False,
            comment=body.comment,
        )
    )


@documents_router.delete(
    "/{id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[AdminOnly]
)
async def delete_document(
    id: int,
    admin: AdminUser,
    session: SessionDep,
    attachments: AttachmentServiceDep,
    background: BackgroundTasks,
):
    storage_key = await document_service.delete_document(
        session, document_id=id, actor_id=admin.id
    )
    # после коммита: фоновые задачи идут уже после ответа
    background.add_task(attachments.s3.delete, storage_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
