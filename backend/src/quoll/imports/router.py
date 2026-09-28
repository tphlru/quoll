"""/api/v1/imports - только админ (В6, import-design §21)"""

from datetime import date
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from quoll.attachments.dependencies import AttachmentServiceDep
from quoll.auth.dependencies import AdminOnly, AdminUser
from quoll.core.exceptions import IdNotExistsException
from quoll.db import SessionDep
from quoll.imports import report, service, template
from quoll.imports.models import BatchStatus
from quoll.imports.schemas import (
    ApplyRequest,
    BatchBrief,
    BatchPatch,
    BatchRead,
    ImportFileRead,
    KindRead,
    RowPatch,
    RowRead,
    RowsRead,
    RowUpdateRead,
    SheetPatch,
)
from quoll.workflows.models import Workflow

imports_router = APIRouter(
    prefix="/api/v1/imports", tags=["Imports"], dependencies=[AdminOnly]
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(
        data,
        media_type=XLSX,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


async def _batch(session, batch_id: int) -> dict:
    return await service.read_batch(session, await service.get_batch(session, batch_id))


@imports_router.get("/kinds", response_model=list[KindRead], summary="Import format")
async def get_kinds(session: SessionDep, workflow_id: int | None = None):
    if workflow_id is not None and await session.get(Workflow, workflow_id) is None:
        raise IdNotExistsException(Workflow.__name__)
    return template.kinds()


@imports_router.get("/template", summary="Import template xlsx with instructions")
async def get_template(kinds: Annotated[list[str] | None, Query()] = None):
    return _xlsx(template.xlsx(kinds), "Шаблон импорта.xlsx")


@imports_router.post(
    "/",
    response_model=BatchRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file and get its preview",
)
async def upload(
    session: SessionDep,
    admin: AdminUser,
    file: Annotated[UploadFile, File()],
    workflow_id: Annotated[int | None, Form()] = None,
):
    batch = await service.upload(session, admin.id, file, workflow_id)
    return await service.read_batch(session, batch)


@imports_router.get("/", response_model=list[BatchBrief], summary="Import batches")
async def list_batches(
    session: SessionDep,
    batch_status: Annotated[BatchStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await service.listing(session, batch_status, limit, offset)


@imports_router.get("/{id}", response_model=BatchRead, summary="Batch with sheets")
async def get_batch(id: int, session: SessionDep):
    return await _batch(session, id)


@imports_router.patch(
    "/{id}", response_model=BatchRead, summary="Workflow, closing stages"
)
async def patch_batch(id: int, body: BatchPatch, session: SessionDep):
    batch = await service.patch_batch(session, id, body)
    return await service.read_batch(session, batch)


@imports_router.patch(
    "/{id}/sheets/{number}", response_model=BatchRead, summary="Sheet kind and columns"
)
async def patch_sheet(id: int, number: int, body: SheetPatch, session: SessionDep):
    batch = await service.patch_sheet(session, id, number, body)
    return await service.read_batch(session, batch)


@imports_router.get("/{id}/rows", response_model=RowsRead, summary="Preview rows")
async def get_rows(
    id: int,
    session: SessionDep,
    sheet: int | None = None,
    row_status: Annotated[list[str] | None, Query(alias="status")] = None,
    group_key: str | None = None,
    has_issues: bool | None = None,
    result: str | None = None,
    limit: Annotated[int, Query(ge=1, le=service.ROWS_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await service.rows(
        session,
        id,
        sheet=sheet,
        status=row_status,
        group_key=group_key,
        has_issues=has_issues,
        result=result,
        limit=limit,
        offset=offset,
    )


@imports_router.get(
    "/{id}/rows/{row_id}", response_model=RowRead, summary="Preview row"
)
async def get_row(id: int, row_id: int, session: SessionDep):
    row = await service.get_row(session, id, row_id)
    return (await service.read_rows(session, [row]))[0]


@imports_router.patch(
    "/{id}/rows/{row_id}", response_model=RowUpdateRead, summary="Edit or exclude a row"
)
async def patch_row(id: int, row_id: int, body: RowPatch, session: SessionDep):
    return await service.patch_row(session, id, row_id, body)


@imports_router.post(
    "/{id}/rows/{row_id}/files",
    response_model=ImportFileRead,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a document file to a registry row",
)
async def upload_file(
    id: int,
    row_id: int,
    session: SessionDep,
    attachments: AttachmentServiceDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()],
    stage_id: Annotated[int | None, Form()] = None,
    title: Annotated[str | None, Form(max_length=255)] = None,
    description: Annotated[str | None, Form()] = None,
    contract_number: Annotated[str | None, Form(max_length=100)] = None,
    contract_signed_at: Annotated[date | None, Form()] = None,
    contract_valid_until: Annotated[date | None, Form()] = None,
):
    values = {
        "kind": kind,
        "stage_id": stage_id,
        "title": title,
        "description": description,
        "contract_number": contract_number,
        "contract_signed_at": contract_signed_at,
        "contract_valid_until": contract_valid_until,
    }
    return await service.upload_file(session, attachments.s3, id, row_id, file, values)


@imports_router.delete(
    "/{id}/rows/{row_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_file(
    id: int,
    row_id: int,
    file_id: int,
    session: SessionDep,
    attachments: AttachmentServiceDep,
):
    keys = await service.delete_file(session, id, row_id, file_id)
    # объект S3 - после коммита: строка без файла хуже, чем файл без строки
    await session.commit()
    await service.delete_keys(attachments.s3, keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@imports_router.post(
    "/{id}/apply",
    response_model=BatchRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start applying the batch in background",
)
async def apply(id: int, body: ApplyRequest, session: SessionDep):
    batch = await service.apply(session, id, body.version)
    return await service.read_batch(session, batch)


@imports_router.post("/{id}/stop", response_model=BatchRead, summary="Stop applying")
async def stop(id: int, session: SessionDep, admin: AdminUser):
    batch = await service.stop(session, id, admin.id)
    return await service.read_batch(session, batch)


@imports_router.post(
    "/{id}/cancel", status_code=status.HTTP_204_NO_CONTENT, summary="Delete the batch"
)
async def cancel(
    id: int, session: SessionDep, admin: AdminUser, attachments: AttachmentServiceDep
):
    keys = await service.cancel(session, id, admin.id)
    await session.commit()
    await service.delete_keys(attachments.s3, keys)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@imports_router.get("/{id}/report", summary="Batch result xlsx")
async def get_report(id: int, session: SessionDep):
    await service.get_batch(session, id)
    return _xlsx(await report.build(session, id), f"Импорт {id}.xlsx")
