from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from quoll.attachments.schemas import AttachmentRead
from quoll.core.schemas import AppBaseModel
from quoll.workflows import step_handlers


# Stage
class StageField(AppBaseModel):
    """поле шага: номер договора, срок лицензии, число обученных"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    label: str = Field(min_length=1)
    type: Literal["string", "text", "date", "datetime", "number", "bool", "contact"]
    required: bool = False
    # правку на пройденном шаге менеджером одобряет руководитель
    approval_after_pass: bool = False
    # явная колонка, куда пишется значение: contract.number, branch.teachers_trained
    bind: str | None = None

    @model_validator(mode="after")
    def _bind_fits(self):
        from quoll.interactions.bindings import BINDINGS

        if self.bind is None:
            return self
        if self.bind not in BINDINGS:
            raise ValueError(f"Unknown bind '{self.bind}'")
        kind = BINDINGS[self.bind][2]
        if self.type not in ({"string", "text"} if kind == "string" else {kind}):
            raise ValueError(f"Bind '{self.bind}' needs type {kind}")
        return self


def _unique_keys(fields: list[StageField] | None) -> list[StageField] | None:
    keys = [f.key for f in fields or []]
    if len(keys) != len(set(keys)):
        raise ValueError("Stage field keys must be unique")
    binds = [f.bind for f in fields or [] if f.bind]
    if len(binds) != len(set(binds)):
        raise ValueError("One column is bound to one field")
    return fields


class StageBase(AppBaseModel):
    name: str
    description: str | None = None
    position: int = 0
    workflow_id: int
    is_terminal: bool
    # флаги веток, как и остальные, после создания не меняются
    is_branch_stage: bool = False
    is_branch_start: bool = False
    # доп. шаг (4.1 «Допсоглашение»); после создания не меняется
    is_side: bool = False
    # особое поведение доп. шага, код из реестра; после создания не меняется
    handler: str | None = Field(default=None, max_length=40)
    # подшаг x.1 шага x; как и флаги, после создания не меняется
    parent_stage_id: int | None = None
    # порог застоя по умолчанию; пусто - на шаге застоя нет
    stall_days: int | None = Field(default=None, ge=1)
    # долгосрочный этап веток: через столько дней без действий - в пассивные
    passive_after_days: int | None = Field(default=None, ge=1)
    fields: Annotated[list[StageField], AfterValidator(_unique_keys)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def check_side(self):
        if self.is_side and (self.is_terminal or self.is_branch_start):
            raise ValueError("Side stage is a working stage")
        if self.handler is not None:
            if self.handler == "":
                raise ValueError("Stage handler cannot be empty")
            if not self.is_side or self.is_branch_stage:
                raise ValueError("Handler belongs to a side stage of the interaction")
        return self

    @model_validator(mode="after")
    def check_branch_start(self):
        if self.is_branch_start and not self.is_branch_stage:
            raise ValueError("Branch start must be a branch stage")
        return self


class StageCreate(StageBase):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_handler_known(self):
        # реестр читается в момент вызова - тесты могут его подменить
        if self.handler is not None and self.handler not in step_handlers.HANDLERS:
            raise ValueError(f"Unknown stage handler '{self.handler}'")
        return self


class StageUpdate(AppBaseModel):
    # флаги стадии после создания не меняются, поэтому их тут нет вовсе
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    position: int | None = None
    stall_days: int | None = Field(default=None, ge=1)
    passive_after_days: int | None = Field(default=None, ge=1)
    # поля шага меняются: правило «флаги не меняются» про слоты и закрытие
    fields: Annotated[list[StageField] | None, AfterValidator(_unique_keys)] = None

    @model_validator(mode="after")
    def _fields_not_null(self):
        if "fields" in self.model_fields_set and self.fields is None:
            raise ValueError("fields cannot be null, send [] to clear")
        return self


class StageRead(StageBase):
    id: int
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class StageHandlerRead(AppBaseModel):
    code: str
    label: str


class StageArchiveRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    # куда переехать заявкам; по умолчанию - предыдущая действующая стадия
    relocate_to_stage_id: int | None = None


# WorkflowTransition
class WorkflowTransitionBase(AppBaseModel):
    name: str
    workflow_id: int
    from_stage_id: int | None = None
    to_stage_id: int
    is_active: bool = True
    comments: str | None = None
    requires_approval: bool = False
    reject_to_stage_id: int | None = None
    is_backward: bool = False
    is_irreversible: bool = False
    required_document_kinds: list[str] = Field(default_factory=list)


class WorkflowTransitionCreate(WorkflowTransitionBase):
    pass


class WorkflowTransitionUpdate(AppBaseModel):
    name: str | None = None
    from_stage_id: int | None = None
    to_stage_id: int | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_nulls_for_required(self):
        # явный null ушёл бы в NOT NULL и вернулся 409 вместо 422
        for field in (
            "name",
            "to_stage_id",
            "is_active",
            "requires_approval",
            "is_backward",
            "is_irreversible",
            "required_document_kinds",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

    comments: str | None = None
    requires_approval: bool | None = None
    reject_to_stage_id: int | None = None
    is_backward: bool | None = None
    is_irreversible: bool | None = None
    required_document_kinds: list[str] | None = None


class WorkflowTransitionRead(WorkflowTransitionBase):
    id: int
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentRead] = Field(default_factory=list)


WorkflowTransitionDetailRead = WorkflowTransitionRead


# Workflow
WarnDays = Annotated[
    list[Annotated[int, Field(ge=1, le=365)]], Field(min_length=1, max_length=5)
]


class WorkflowBase(AppBaseModel):
    name: str
    description: str | None = None
    # за сколько дней предупреждать о конце лицензии и договора (Т-3)
    warn_days: WarnDays = Field(default_factory=lambda: [60, 30])


class WorkflowCreate(WorkflowBase):
    pass


class WorkflowUpdate(AppBaseModel):
    name: str | None = None
    description: str | None = None
    warn_days: WarnDays | None = None


class WorkflowRead(WorkflowBase):
    id: int
    is_published: bool
    created_at: datetime
    updated_at: datetime


class WorkflowDetailRead(WorkflowRead):
    stages: list[StageRead] = Field(default_factory=list)
    transitions: list[WorkflowTransitionRead] = Field(default_factory=list)


class StartStageRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_id: int


class WorkflowChangeCreate(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: int
    text: str = Field(min_length=1)


class WorkflowChangeDecision(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = None


class WorkflowChangeRead(AppBaseModel):
    id: int
    workflow_id: int
    requested_by: str | None
    text: str
    status: str
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str | None
    created_at: datetime
