from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from quoll.attachments.schemas import AttachmentRead
from quoll.catalog.schemas import UniversityRead
from quoll.core.schemas import AppBaseModel
from quoll.workflows.schemas import StageRead, WorkflowRead


class BranchWrite(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    program_id: int
    # не больше одного продукта, и только из продуктов программы
    product_id: int | None = None


# Interaction
class InteractionCreate(AppBaseModel):
    # лишнее поле - 422, а не молчаливый игнор: иначе PATCH с owner_id
    # ответит 200 и ничего не сделает
    model_config = ConfigDict(extra="forbid")

    university_id: int
    # шаг 0: ИТ-программы с продуктами - ветки-черновики состава
    branches: list[BranchWrite] = Field(min_length=1)
    planned_date: date | None = None
    # только опубликованный - по черновику графа заявке ехать нельзя
    workflow_id: int | None = None


class InteractionUpdate(AppBaseModel):
    """владелец, стадия и воркфлоу меняются только операциями над заявкой"""

    model_config = ConfigDict(extra="forbid")

    university_id: int | None = None
    planned_date: date | None = None


class InteractionSettings(AppBaseModel):
    """пороги руководителя (Т-3): застой по шагам {stage_id: дней, null -
    снять} и сроки предупреждений (null - как у воркфлоу)"""

    model_config = ConfigDict(extra="forbid")

    stall_overrides: dict[int, Annotated[int, Field(ge=1)] | None] | None = None
    warn_days: (
        Annotated[
            list[Annotated[int, Field(ge=1, le=365)]], Field(min_length=1, max_length=5)
        ]
        | None
    ) = None


class AssignRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    manager_id: str
    # обязательно, но может быть null - «ожидаю, что владельца нет».
    # Отсутствие поля - 422, чтобы «забыл передать» не стало «владельца нет»
    expected_owner_id: str | None
    reason: str | None = None


class AcceptRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    # начальная стадия воркфлоу - у заявки без воркфлоу она его и задаёт
    to_stage_id: int
    comment: str | None = None


class DocumentDecision(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = None


class RollbackRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage_id: int
    expected_state_id: int
    comment: str = Field(min_length=1)


class CommentRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1)


class TransitionRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage_id: int
    # обязательно, для черновика - null, как expected_owner_id у назначения
    expected_state_id: int | None
    comment: str | None = None


class ReopenRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    manager_id: str
    to_stage_id: int
    expected_owner_id: str | None
    comment: str = Field(min_length=1)


class CloseRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage_id: int
    expected_state_id: int | None
    # причина из справочника; комментарий обязателен у «другое» (Д9)
    close_reason_id: int
    # у подписанной с открытыми ветками: «закрыть все ветки и завершить»
    branch_close_reason_id: int | None = None
    comment: str | None = None


class ReasonedRequest(AppBaseModel):
    """досрочное закрытие ветки (остаётся на своём шаге, П6) или отмена
    черновика"""

    model_config = ConfigDict(extra="forbid")

    close_reason_id: int
    comment: str | None = None


class PauseRequest(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    # до какого момента; null - бессрочно
    until: datetime | None = None
    comment: str = Field(min_length=1)


class InteractionRead(AppBaseModel):
    id: int
    university_id: int
    workflow_id: int | None
    state_id: int | None
    owner_id: str | None
    created_by: str | None
    pause_state: str
    paused_until: datetime | None
    pause_comment: str | None
    close_reason_id: int | None
    # вычисляемое: черновик / ждёт принятия / в работе / на паузе / подписан / закрыта
    status: str
    no_return_at: datetime | None
    # активный слот входит в предел КАМа, пассивный - нет (Д19)
    slot: str
    slot_changed_at: datetime | None
    stall_since: datetime | None
    stall_overrides: dict[str, int]
    warn_days: list[int] | None
    planned_date: date | None
    signed_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class InteractionDetailRead(InteractionRead):
    university: UniversityRead
    workflow: WorkflowRead | None = None
    state: StageRead | None = None


class StageHistoryRead(AppBaseModel):
    from_stage_id: int | None
    to_stage_id: int | None
    transition_id: int | None
    kind: str
    actor_id: str | None
    comment: str | None
    # ход ветки продукта; null - ход самого взаимодействия
    branch_id: int | None
    # прохождение: null - основной указатель, иначе доп.
    side_pointer_id: int | None
    payload: dict[str, Any]
    created_at: datetime


class AssignmentRead(AppBaseModel):
    manager_id: str | None
    assigned_at: datetime
    released_at: datetime | None
    reason: str | None


class InteractionHistoryRead(AppBaseModel):
    """два списка, а не одна лента: у записей разные поля, по времени их
    сливает клиент"""

    stages: list[StageHistoryRead]
    assignments: list[AssignmentRead]


class RequestCreate(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["TRANSFER", "CLOSE", "TRANSITION"]
    # аппрув шага или закрытие ветки
    branch_id: int | None = None
    # у закрытия заявки - куда закрыть; у передачи - кому, но это лишь предложение
    target_stage_id: int | None = None
    target_manager_id: str | None = None
    close_reason_id: int | None = None
    # закрытие подписанной заявки закрывает и открытые ветки - с этой причиной
    branch_close_reason_id: int | None = None
    # аппрув перехода доп. указателя
    side_pointer_id: int | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _targets_fit_kind(self):
        if self.kind == "CLOSE" and (
            (self.target_stage_id is None) == (self.branch_id is None)
            or self.target_manager_id
            or self.close_reason_id is None
        ):
            raise ValueError(
                "CLOSE needs close_reason_id and target_stage_id for the "
                "interaction or branch_id for a branch"
            )
        if self.kind != "CLOSE" and self.close_reason_id is not None:
            raise ValueError("close_reason_id is only for CLOSE")
        if self.branch_close_reason_id is not None and (
            self.kind != "CLOSE" or self.branch_id is not None
        ):
            raise ValueError("branch_close_reason_id is for closing the interaction")
        if self.kind == "TRANSFER" and self.target_stage_id is not None:
            raise ValueError("TRANSFER has no target_stage_id")
        if self.kind == "TRANSITION" and (
            self.target_stage_id is None or self.target_manager_id
        ):
            raise ValueError(
                "TRANSITION needs target_stage_id and no target_manager_id"
            )
        if self.side_pointer_id is not None and (
            self.kind != "TRANSITION" or self.branch_id is not None
        ):
            raise ValueError("Side pass asks only for a transition")
        return self


class RequestApprove(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    # у передачи обязателен: решает руководитель, а не предложение менеджера
    target_manager_id: str | None = None
    comment: str | None = None


class RequestReject(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1)


class RequestRead(AppBaseModel):
    id: int
    interaction_id: int
    kind: str
    status: str
    requested_by: str | None
    from_owner_id: str | None
    target_stage_id: int | None
    target_manager_id: str | None
    transition_id: int | None
    branch_id: int | None
    close_reason_id: int | None
    branch_close_reason_id: int | None
    side_pointer_id: int | None
    reason: str
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str | None
    created_at: datetime


class DocumentRead(AppBaseModel):
    id: int
    interaction_id: int
    stage_id: int
    uploaded_by: str | None
    branch_id: int | None
    replaces_document_id: int | None
    title: str
    kind: str
    description: str | None
    contract_number: str | None
    contract_signed_at: date | None
    contract_valid_until: date | None
    supplementary_agreement_id: int | None
    side_pointer_id: int | None
    metadata: dict[str, Any]
    # прежние версии не пропадают, а перестают быть актуальными
    is_current: bool
    # где лежит действующая новая версия - в том числе на подшаге
    replaced_by_id: int | None
    replaced_on_stage_id: int | None
    # ACTIVE, PENDING - ждёт руководителя, REJECTED
    status: str
    created_at: datetime
    attachment: AttachmentRead


class InteractionImport(AppBaseModel):
    university_name: str
    vendor_name: str
    it_program: str
    it_product: str | None = None
    contract_number: str
    license_singed: bool
    license_expired_at: int
    manager_full_name: str
    comment: str


class InteractionImportError(AppBaseModel):
    """одна ошибка валидации строки импорта: колонка + понятный текст"""

    column: str
    message: str


class InteractionImportValidationError(AppBaseModel):
    """человекочитаемый результат pydantic ValidationError"""

    errors: list[InteractionImportError]

    @classmethod
    def from_validation_error(
        cls, exc: ValidationError
    ) -> "InteractionImportValidationError":
        return cls(
            errors=[
                InteractionImportError(
                    column=".".join(str(part) for part in err["loc"]),
                    message=err["msg"],
                )
                for err in exc.errors()
            ]
        )


class InteractionImportRow(AppBaseModel):
    error: InteractionImportValidationError | None = None
    interaction_import: InteractionImport | None


class InteractionImportAction(InteractionImportRow):
    action: str | None


class InteractionImportResult(AppBaseModel):
    """результат импорта: ошибки и удавшиеся операции, по номеру строки"""

    errors: dict[int, InteractionImportValidationError]
    imported: dict[int, InteractionImportAction]


class StageValuesWrite(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    # целиком, не слиянием: убрать поле - прислать без него
    values: dict[str, Any]


class StageValuesRead(AppBaseModel):
    stage_id: int
    branch_id: int | None
    side_pointer_id: int | None
    values: dict[str, Any]
    updated_by: str | None
    updated_at: datetime
    # правка пройденного шага на одобрении у руководителя
    pending_values: dict[str, Any] | None
    pending_by: str | None


class ContractStatusWrite(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_status: Literal["PROPOSED", "APPROVED", "REJECTED"]


class BranchRead(AppBaseModel):
    id: int
    interaction_id: int
    program_id: int | None
    product_id: int | None
    contract_status: str
    origin: str
    supplementary_agreement_id: int | None
    # итерация пары программа+продукт (Д21)
    iteration: int
    # null - черновик состава, до подписания
    state_id: int | None
    opened_at: datetime | None
    closed_at: datetime | None
    close_reason_id: int | None
    stall_since: datetime | None
    pause_state: str
    paused_until: datetime | None
    pause_comment: str | None
    license_signed_at: date | None
    license_term_years: int | None
    license_until: date | None
    transfer_status: str
    teachers_trained: int | None
    added_by: str | None
    created_at: datetime


# допсоглашение (шаг 4.1)

_SHAPES = {
    # тип -> (обязательные, допустимые)
    "NEW_BRANCH": ({"program_id"}, {"program_id", "product_id"}),
    "EXTEND_LICENSE": ({"branch_id", "license_until"}, {"branch_id", "license_until"}),
    "RESUME": ({"branch_id"}, {"branch_id"}),
    "EXCLUDE": ({"branch_id"}, {"branch_id"}),
    "EXTEND_CONTRACT": ({"contract_valid_until"}, {"contract_valid_until"}),
}


class AgreementActionWrite(AppBaseModel):
    """форма действия по типу - то же, что CHECK chk_sa_action_shape"""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "NEW_BRANCH", "EXTEND_LICENSE", "RESUME", "EXCLUDE", "EXTEND_CONTRACT"
    ]
    branch_id: int | None = None
    program_id: int | None = None
    product_id: int | None = None
    license_until: date | None = None
    contract_valid_until: date | None = None

    @model_validator(mode="after")
    def _shape(self):
        required, allowed = _SHAPES[self.type]
        given = {
            name
            for name in (
                "branch_id",
                "program_id",
                "product_id",
                "license_until",
                "contract_valid_until",
            )
            if getattr(self, name) is not None
        }
        if missing := required - given:
            raise ValueError(f"{self.type} needs {sorted(missing)}")
        if extra := given - allowed:
            raise ValueError(f"{self.type} does not take {sorted(extra)}")
        return self


class AgreementActionRead(AppBaseModel):
    id: int
    type: str
    branch_id: int | None
    program_id: int | None
    product_id: int | None
    license_until: date | None
    contract_valid_until: date | None
    result_branch_id: int | None


class AgreementUpdate(AppBaseModel):
    """null очищает; номер и дата необязательны (Д20)"""

    model_config = ConfigDict(extra="forbid")

    number: str | None = Field(default=None, max_length=100)
    signed_at: date | None = None


class AgreementRead(AppBaseModel):
    id: int
    interaction_id: int
    number: str | None
    signed_at: date | None
    status: str
    created_by: str | None
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_comment: str | None
    side_pointer_id: int | None
    scan_document_id: int | None
    pending_request_id: int | None
    actions: list[AgreementActionRead]


# доп. указатель (Д30-Д50)


class SidePointerStart(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_id: int
    comment: str | None = None


class SidePointerTransition(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage_id: int
    expected_state_id: int | None
    comment: str | None = None


class SidePointerCancel(AppBaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = None


class SidePointerRead(AppBaseModel):
    id: int
    interaction_id: int
    branch_id: int | None
    entry_stage_id: int
    stage_id: int
    status: str
    started_by: str | None
    started_at: datetime
    finished_by: str | None
    finished_at: datetime | None
    finish_comment: str | None
