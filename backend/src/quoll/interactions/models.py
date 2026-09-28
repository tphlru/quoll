from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    case,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quoll.core.mixins import IdMixin, TimestampMixin
from quoll.db import Base, created_at_dt, str_255
from quoll.workflows.models import Stage, Workflow


class PauseState(StrEnum):
    """состояния паузы заявки"""

    ACTIVE = "ACTIVE"
    PAUSED_MANUAL = "PAUSED_MANUAL"
    PAUSED_TIMED = "PAUSED_TIMED"
    EXPIRED_WAITING_CAPACITY = "EXPIRED_WAITING_CAPACITY"


class University(Base, IdMixin, TimestampMixin):
    """вуз; ключ - ИНН + КПП, а не название. Филиал со своим договором -
    отдельный вуз с тем же ИНН и своим КПП (М 3.1)"""

    __table_args__ = (
        Index(
            "uq_universities_inn_kpp",
            "inn",
            "kpp",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    # как в ЕГРЮЛ - для договора
    full_name: Mapped[str] = mapped_column(Text)
    # для интерфейса, отчётов и поиска
    short_name: Mapped[str_255] = mapped_column(index=True)
    inn: Mapped[str] = mapped_column(String(10))
    kpp: Mapped[str | None] = mapped_column(String(9), nullable=True)
    site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str_255]
    city: Mapped[str_255]

    interactions: Mapped[list["Interaction"]] = relationship(
        back_populates="university"
    )


class Vendor(Base, IdMixin, TimestampMixin):
    """компания, выпускающая продукт; контакты - в общем справочнике"""

    name: Mapped[str_255] = mapped_column(unique=True, index=True)
    site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # «Дочерняя», «ПАО (материнская)» - свободная пометка
    kind: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SlotKind(StrEnum):
    ACTIVE = "ACTIVE"
    PASSIVE = "PASSIVE"


class InteractionStatus(StrEnum):
    """состояние заявки для людей (М 3.11); первое сработавшее"""

    CLOSED = "CLOSED"
    DRAFT = "DRAFT"  # ни стадии, ни владельца
    AWAITING_ACCEPTANCE = "AWAITING_ACCEPTANCE"  # предложена КАМу
    PAUSED = "PAUSED"
    SIGNED = "SIGNED"  # договор подписан
    IN_PROGRESS = "IN_PROGRESS"


class Interaction(Base, IdMixin, TimestampMixin):
    __table_args__ = (
        # стадия должна быть из того же воркфлоу, что и заявка.
        # составной ключ с NULL не проверяется, поэтому пару
        # "стадия есть, воркфлоу нет" ловит CHECK ниже
        ForeignKeyConstraint(
            ["state_id", "workflow_id"],
            ["stages.id", "stages.workflow_id"],
            ondelete="RESTRICT",
            name="fk_interactions_state_id_workflow_id_stages",
        ),
        CheckConstraint(
            "state_id IS NULL OR workflow_id IS NOT NULL",
            name="chk_interaction_stage_requires_workflow",
        ),
        CheckConstraint(
            "(pause_state = 'ACTIVE' AND is_paused = FALSE AND paused_until IS NULL) OR "
            "(pause_state = 'PAUSED_MANUAL' AND is_paused = TRUE AND paused_until IS NULL) OR "
            "(pause_state = 'PAUSED_TIMED' AND is_paused = TRUE AND paused_until IS NOT NULL) OR "
            "(pause_state = 'EXPIRED_WAITING_CAPACITY' AND is_paused = TRUE AND paused_until IS NULL)",
            name="chk_interaction_pause_state",
        ),
        CheckConstraint("slot IN ('ACTIVE', 'PASSIVE')", name="chk_interaction_slot"),
        # в пассивные - только после подписания
        CheckConstraint(
            "slot = 'ACTIVE' OR no_return_at IS NOT NULL",
            name="chk_interaction_passive_after_signing",
        ),
        # подсчёт ёмкости
        Index(
            "ix_interactions_active_slots",
            "owner_id",
            postgresql_where=text("slot = 'ACTIVE' AND closed_at IS NULL"),
        ),
        # у вуза не больше одной незакрытой заявки (М 4)
        Index(
            "uq_interactions_open_per_university",
            "university_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    university_id: Mapped[int] = mapped_column(
        ForeignKey("universities.id", ondelete="RESTRICT"), index=True
    )

    # RESTRICT, а не SET NULL - иначе обнуление порвёт составной ключ и CHECK
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    state_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # только на managers, владельцем заявки может быть лишь менеджер,
    # а RESTRICT не даёт удалить менеджера с заявками
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("managers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # руководитель, создавший заявку. Пока владельца нет, это его черновик
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # кто владел раньше - авторство переживает смену роли менеджера
    last_owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    is_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    pause_state: Mapped[str] = mapped_column(
        String(30),
        default=PauseState.ACTIVE,
        server_default=PauseState.ACTIVE,
        index=True,
    )
    paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # причина текущей паузы, история пауз - в журнале
    pause_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # застой: от этого момента; пороги руководителя по шагам {stage_id: дней}
    stall_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stall_overrides: Mapped[dict[str, int]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # сроки предупреждений руководителя; пусто - как у воркфлоу
    warn_days: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    # активный слот считается в предел КАМа, пассивный - нет (Д19)
    slot: Mapped[str] = mapped_column(
        String(10), default=SlotKind.ACTIVE, server_default=SlotKind.ACTIVE
    )
    slot_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # единственный признак закрытой: терминальная стадия или отменённый черновик
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # флажок «договор подписан» на шаге 4 (Д13); по нему «подписано за период»
    signed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # точка невозврата пройдена (4 -> 5): ветки открыты, состав зафиксирован
    no_return_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    planned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # у досрочного закрытия и отмены; штатное завершение по ребру - без причины
    close_reason_id: Mapped[int | None] = mapped_column(
        ForeignKey("close_reasons.id", ondelete="RESTRICT"), nullable=True
    )

    @hybrid_property
    def status(self) -> str:
        if self.closed_at is not None:
            return InteractionStatus.CLOSED
        if self.state_id is None:
            return (
                InteractionStatus.DRAFT
                if self.owner_id is None
                else InteractionStatus.AWAITING_ACCEPTANCE
            )
        if self.is_paused:
            return InteractionStatus.PAUSED
        if self.signed_at is not None:
            return InteractionStatus.SIGNED
        return InteractionStatus.IN_PROGRESS

    @status.inplace.expression
    @classmethod
    def _status_expression(cls):
        # то же для фильтра в SQL - иначе пагинация по статусу врала бы
        return case(
            (cls.closed_at.is_not(None), InteractionStatus.CLOSED.value),
            (
                cls.state_id.is_(None) & cls.owner_id.is_(None),
                InteractionStatus.DRAFT.value,
            ),
            (cls.state_id.is_(None), InteractionStatus.AWAITING_ACCEPTANCE.value),
            (cls.is_paused.is_(True), InteractionStatus.PAUSED.value),
            (cls.signed_at.is_not(None), InteractionStatus.SIGNED.value),
            else_=InteractionStatus.IN_PROGRESS.value,
        )

    # Relationships
    university: Mapped[University] = relationship(back_populates="interactions")
    workflow: Mapped[Workflow | None] = relationship()
    # viewonly, иначе эта связь и workflow спорят за право писать workflow_id
    state: Mapped[Stage | None] = relationship(viewonly=True)


class StageChangeKind(StrEnum):
    """как заявка попала на стадию"""

    TRANSITION = "TRANSITION"  # по ребру графа
    CLOSE = "CLOSE"  # досрочное закрытие, без ребра
    REOPEN = "REOPEN"  # переоткрытие закрытой
    RELOCATION = "RELOCATION"  # перенос при архивации стадии
    REJECTION = "REJECTION"  # отказ в аппруве увёл на доработку
    ROLLBACK = "ROLLBACK"  # откат руководителем на несколько шагов
    CANCEL = "CANCEL"  # отмена черновика, стадии нет
    PAUSE = "PAUSE"
    UNPAUSE = "UNPAUSE"
    BRANCH_ADDED = "BRANCH_ADDED"
    BRANCH_REMOVED = "BRANCH_REMOVED"
    SA_OPENED = "SA_OPENED"
    SA_APPROVED = "SA_APPROVED"
    SA_REJECTED = "SA_REJECTED"
    LICENSE_EXTENDED = "LICENSE_EXTENDED"
    CONTRACT_EXTENDED = "CONTRACT_EXTENDED"
    IMPORT = "IMPORT"
    COMMENT = "COMMENT"
    SA_CANCELLED = "SA_CANCELLED"
    SA_RETURNED = "SA_RETURNED"  # допсоглашение вернулось в черновик
    RESTART = "RESTART"  # завершённая ветка вернулась на начало веток (Д21)
    SLOT_PASSIVE = "SLOT_PASSIVE"  # увели в пассивные слоты (Д19)
    SLOT_ACTIVE = "SLOT_ACTIVE"
    SIDE_STARTED = "SIDE_STARTED"
    SIDE_FINISHED = "SIDE_FINISHED"
    SIDE_CANCELLED = "SIDE_CANCELLED"


class InteractionStageHistory(Base):
    """история движения заявки по стадиям - бизнес-история, не журнал.

    ссылки на стадии и ребро RESTRICT: стадии не удаляются, а архивируются,
    и история не должна терять, откуда и куда шла заявка
    """

    __tablename__ = "interaction_stage_history"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in StageChangeKind) + ")",
            name="chk_stage_history_kind",
        ),
        Index("ix_stage_history_interaction_created", "interaction_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )
    # NULL - заявка встала на первую стадию из черновика; у событий без
    # движения (отмена черновика, пауза до принятия) пусты обе
    from_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT"), nullable=True
    )
    to_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT"), nullable=True
    )
    # NULL у закрытия, переоткрытия и переноса - они идут не по ребру
    transition_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_transitions.id", ondelete="RESTRICT"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ход ветки продукта; NULL - ход самого взаимодействия
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # прохождение: NULL - основной указатель, иначе конкретный доп. указатель
    side_pointer_id: Mapped[int | None] = mapped_column(
        ForeignKey("side_pointers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # структура события для отчётов: {sa_id, old, new, ...}
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[created_at_dt]


class InteractionAssignment(Base):
    """кто и когда вёл заявку. Нужна, чтобы менеджер видел бывшие свои -
    last_owner_id помнит только один шаг назад"""

    __table_args__ = (
        # открытая запись одна - та, что совпадает с текущим владельцем
        Index(
            "uq_interaction_assignments_open",
            "interaction_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    manager_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_at: Mapped[created_at_dt]
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SidePointerStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class SidePointer(Base):
    """доп. указатель: проходит доп. шаги, пока основной уже ушёл дальше.

    своих блокировок нет - прикрывает Interaction, как ветки и ДС
    """

    __tablename__ = "side_pointers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'FINISHED', 'CANCELLED')",
            name="chk_side_pointer_status",
        ),
        CheckConstraint(
            "(status = 'ACTIVE') = (finished_at IS NULL)",
            name="chk_side_pointer_finished",
        ),
        # одновременно активен один доп. указатель заявки (I1)
        Index(
            "uq_side_pointers_active",
            "interaction_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    # каркас Д40: доп. указатель ветки, сейчас всегда NULL
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    entry_stage_id: Mapped[int] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT")
    )
    # где стоит; у завершённого - последний доп. шаг
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(
        String(20),
        default=SidePointerStatus.ACTIVE,
        server_default=SidePointerStatus.ACTIVE,
    )
    started_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finish_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class RequestKind(StrEnum):
    TRANSFER = "TRANSFER"
    CLOSE = "CLOSE"
    # аппрув перехода по ребру с requires_approval
    TRANSITION = "TRANSITION"


class RequestStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class InteractionRequest(Base):
    """просьба менеджера руководителю: передать проект или закрыть досрочно.
    Решает руководитель - кому передать и закрывать ли (П8)"""

    __table_args__ = (
        CheckConstraint(
            "kind IN ('TRANSFER', 'CLOSE', 'TRANSITION')",
            name="chk_request_kind",
        ),
        # у перехода - ребро и его цель; у остальных ребра нет
        CheckConstraint(
            "(kind = 'TRANSITION') = (transition_id IS NOT NULL)",
            name="chk_request_transition_edge",
        ),
        CheckConstraint(
            "kind <> 'TRANSITION' OR (target_stage_id IS NOT NULL "
            "AND target_manager_id IS NULL)",
            name="chk_request_transition_target",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="chk_request_status",
        ),
        # у закрытия цель - стадия, у передачи - предложение менеджера, не стадия
        # закрытие заявки - в терминальную стадию; ветки - без стадии,
        # она остаётся на своём шаге (П6). Причина - всегда
        CheckConstraint(
            "kind <> 'CLOSE' OR (target_manager_id IS NULL "
            "AND close_reason_id IS NOT NULL "
            "AND (branch_id IS NULL) = (target_stage_id IS NOT NULL))",
            name="chk_request_close_target",
        ),
        CheckConstraint(
            "kind <> 'TRANSFER' OR target_stage_id IS NULL",
            name="chk_request_transfer_target",
        ),
        # решено ровно тогда, когда не ждёт. По времени, а не по decided_by -
        # его обнулит удаление пользователя
        CheckConstraint(
            "(status = 'PENDING') = (decided_at IS NULL)",
            name="chk_request_decided",
        ),
        # аппрувы разных веток и разных прохождений ждут параллельно
        Index(
            "uq_interaction_requests_pending",
            "interaction_id",
            "kind",
            "branch_id",
            "side_pointer_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "branch_id IS NULL OR kind IN ('TRANSITION', 'CLOSE')",
            name="chk_request_branch_only_transition",
        ),
        CheckConstraint(
            "side_pointer_id IS NULL OR (kind = 'TRANSITION' AND branch_id IS NULL)",
            name="chk_request_side_only_transition",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # владелец на момент подачи - сменился, и просьба устарела
    from_owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_manager_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT"), nullable=True
    )
    transition_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_transitions.id", ondelete="RESTRICT"), nullable=True
    )
    # аппрув шага или закрытие ветки
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    close_reason_id: Mapped[int | None] = mapped_column(
        ForeignKey("close_reasons.id", ondelete="RESTRICT"), nullable=True
    )
    # «закрыть все ветки и завершить»: причина для открытых веток
    branch_close_reason_id: Mapped[int | None] = mapped_column(
        ForeignKey("close_reasons.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default=RequestStatus.PENDING, server_default="PENDING"
    )
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # прохождение: NULL - основной указатель, иначе конкретный доп. указатель
    side_pointer_id: Mapped[int | None] = mapped_column(
        ForeignKey("side_pointers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[created_at_dt]


class InteractionDocument(Base):
    """файл проекта, приложенный на стадии заявки. Новая версия ссылается на
    прежнюю, прежняя остаётся - «уходит вниз и сереет»"""

    __table_args__ = (
        # цель составного ключа ниже: версия - только из той же заявки
        UniqueConstraint("id", "interaction_id", name="uq_documents_id_interaction"),
        ForeignKeyConstraint(
            ["replaces_document_id", "interaction_id"],
            ["interaction_documents.id", "interaction_documents.interaction_id"],
            # только ссылку: interaction_id обнулять нельзя
            ondelete="SET NULL (replaces_document_id)",
            name="fk_documents_replaces_same_interaction",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'PENDING', 'REJECTED')", name="chk_document_status"
        ),
        CheckConstraint(
            "kind <> 'OTHER' OR description IS NOT NULL",
            name="chk_document_other_described",
        ),
        CheckConstraint(
            "(supplementary_agreement_id IS NOT NULL) = "
            "(kind = 'SUPPLEMENTARY_AGREEMENT')",
            name="chk_document_sa_scan",
        ),
        CheckConstraint(
            "kind = 'CONTRACT' OR (contract_number IS NULL AND "
            "contract_signed_at IS NULL AND contract_valid_until IS NULL)",
            name="chk_document_contract_fields",
        ),
        # у версии один преемник - иначе цепочка раздвоится
        Index(
            "uq_documents_replaces",
            "replaces_document_id",
            unique=True,
            postgresql_where=text("replaces_document_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    # документ без файла бессмыслен, и одно вложение - один документ
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), unique=True
    )
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id", ondelete="RESTRICT"))
    uploaded_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    replaces_document_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # внутреннее название - не имя файла: «Договор №12» вместо scan_0042.pdf
    title: Mapped[str_255]
    # вид из справочника: по нему переход проверяет нужные файлы
    kind: Mapped[str] = mapped_column(
        ForeignKey("document_kinds.code", ondelete="RESTRICT"),
        index=True,
    )
    # что за файл; обязательно у вида «другое»
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # реквизиты - только у договора; новая версия копирует их, если не заданы
    contract_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contract_signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # metadata в декларативной модели занято самим SQLAlchemy
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # файл менеджера не на текущий шаг ждёт руководителя; отклонённый
    # остаётся в списке, но из цепочки версий выходит
    status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", server_default="ACTIVE"
    )
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    # скан допсоглашения
    supplementary_agreement_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplementary_agreements.id"), nullable=True, index=True
    )
    # прохождение: NULL - основной указатель, иначе конкретный доп. указатель
    side_pointer_id: Mapped[int | None] = mapped_column(
        ForeignKey("side_pointers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[created_at_dt]


class DocumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    REJECTED = "REJECTED"


class InteractionStageValues(Base):
    """значения полей шага: одна строка на заявку и стадию. История правок -
    в журнале, поэтому не EAV и не версии"""

    __tablename__ = "interaction_stage_values"
    __table_args__ = (
        Index(
            "uq_stage_values_pass",
            "interaction_id",
            "stage_id",
            "branch_id",
            "side_pointer_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id", ondelete="RESTRICT"))
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    # прохождение: NULL - основной указатель, иначе конкретный доп. указатель
    side_pointer_id: Mapped[int | None] = mapped_column(
        ForeignKey("side_pointers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    values: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
    # правка пройденного шага, ждущая руководителя; действуют values
    pending_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    pending_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ContractStatus(StrEnum):
    """ветка в составе договора до подписания"""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BranchOrigin(StrEnum):
    CONTRACT = "CONTRACT"
    SUPPLEMENTARY_AGREEMENT = "SUPPLEMENTARY_AGREEMENT"


class TransferStatus(StrEnum):
    NOT_TRANSFERRED = "NOT_TRANSFERRED"
    TRANSFERRED = "TRANSFERRED"


class Branch(Base):
    """ветка = ИТ-программа и не больше одного её продукта (М 3.12).

    до подписания - черновик состава (стадии нет), при подписании одобренные
    встают на начало шагов веток, и шаги 5-8 идут по каждой отдельно.
    Своих блокировок нет - всё под блокировкой взаимодействия
    """

    __tablename__ = "branches"
    __table_args__ = (
        # пара программа+продукт повторяется итерациями (Д21)
        Index(
            "uq_branches_interaction_program_product",
            "interaction_id",
            "program_id",
            "product_id",
            "iteration",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        # но живая ветка у пары одна: черновик состава или открытая
        Index(
            "uq_branches_live_pair",
            "interaction_id",
            "program_id",
            "product_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
        CheckConstraint(
            "(origin = 'SUPPLEMENTARY_AGREEMENT') = "
            "(supplementary_agreement_id IS NOT NULL)",
            name="chk_branch_origin_sa",
        ),
        CheckConstraint(
            "contract_status IN ('PROPOSED', 'APPROVED', 'REJECTED')",
            name="chk_branch_contract_status",
        ),
        CheckConstraint(
            "state_id IS NULL OR contract_status = 'APPROVED'",
            name="chk_branch_on_stage_is_approved",
        ),
        CheckConstraint(
            "transfer_status IN ('NOT_TRANSFERRED', 'TRANSFERRED')",
            name="chk_branch_transfer_status",
        ),
        CheckConstraint(
            "(pause_state = 'ACTIVE' AND paused_until IS NULL) OR "
            "(pause_state = 'PAUSED_MANUAL' AND paused_until IS NULL) OR "
            "(pause_state = 'PAUSED_TIMED' AND paused_until IS NOT NULL)",
            name="chk_branch_pause_state",
        ),
        CheckConstraint(
            "closed_at IS NULL OR pause_state = 'ACTIVE'",
            name="chk_branch_closed_not_paused",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    # пусто только у импорта (М 3.12)
    program_id: Mapped[int | None] = mapped_column(
        ForeignKey("it_programs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    contract_status: Mapped[str] = mapped_column(
        String(20),
        default=ContractStatus.PROPOSED,
        server_default=ContractStatus.PROPOSED,
    )
    origin: Mapped[str] = mapped_column(
        String(30), default=BranchOrigin.CONTRACT, server_default=BranchOrigin.CONTRACT
    )
    supplementary_agreement_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplementary_agreements.id"), nullable=True
    )
    iteration: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    state_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # подписание договора или одобрение допсоглашения - от него считается жизнь ветки
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    close_reason_id: Mapped[int | None] = mapped_column(
        ForeignKey("close_reasons.id", ondelete="RESTRICT"), nullable=True
    )
    # закрыта вместе с заявкой - переоткрытие заявки её вернёт
    closed_with_interaction: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    stall_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # своя пауза ветки; слот КАМа не трогает (10.2/16)
    pause_state: Mapped[str] = mapped_column(
        String(30), default=PauseState.ACTIVE, server_default=PauseState.ACTIVE
    )
    paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pause_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # лицензия: until = подписание + срок, продление меняет только until
    license_signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    license_term_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    license_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    transfer_status: Mapped[str] = mapped_column(
        String(20),
        default=TransferStatus.NOT_TRANSFERRED,
        server_default=TransferStatus.NOT_TRANSFERRED,
    )
    teachers_trained: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[created_at_dt]


class AgreementStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"


class SupplementaryAgreement(Base):
    """допсоглашение (М 3.13, 6.1): единственный способ менять договор после
    подписания. Своих блокировок нет - прикрывает взаимодействие"""

    __tablename__ = "supplementary_agreements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'PENDING', 'APPROVED', 'CANCELLED')",
            name="chk_sa_status",
        ),
        CheckConstraint(
            "(status IN ('APPROVED', 'CANCELLED')) = (decided_at IS NOT NULL)",
            name="chk_sa_decided",
        ),
        # незавершённое у заявки одно - «второй указатель» (М 3.11)
        Index(
            "uq_sa_open_per_interaction",
            "interaction_id",
            unique=True,
            postgresql_where=text("status IN ('DRAFT', 'PENDING')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    # прохождение, чей шаг 4.1 держит это ДС: NULL - основной указатель
    side_pointer_id: Mapped[int | None] = mapped_column(
        ForeignKey("side_pointers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=AgreementStatus.DRAFT, server_default=AgreementStatus.DRAFT
    )
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at_dt]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class ActionType(StrEnum):
    NEW_BRANCH = "NEW_BRANCH"
    EXTEND_LICENSE = "EXTEND_LICENSE"
    RESUME = "RESUME"
    EXCLUDE = "EXCLUDE"
    EXTEND_CONTRACT = "EXTEND_CONTRACT"


class AgreementAction(Base):
    """что сделать при одобрении допсоглашения"""

    __tablename__ = "sa_actions"
    __table_args__ = (
        CheckConstraint(
            "(type = 'NEW_BRANCH' AND program_id IS NOT NULL AND branch_id IS NULL "
            "AND license_until IS NULL AND contract_valid_until IS NULL) OR "
            "(type = 'EXTEND_LICENSE' AND branch_id IS NOT NULL "
            "AND license_until IS NOT NULL AND program_id IS NULL "
            "AND product_id IS NULL AND contract_valid_until IS NULL) OR "
            "(type IN ('RESUME', 'EXCLUDE') AND branch_id IS NOT NULL "
            "AND program_id IS NULL AND product_id IS NULL "
            "AND license_until IS NULL AND contract_valid_until IS NULL) OR "
            "(type = 'EXTEND_CONTRACT' AND contract_valid_until IS NOT NULL "
            "AND branch_id IS NULL AND program_id IS NULL AND product_id IS NULL "
            "AND license_until IS NULL)",
            name="chk_sa_action_shape",
        ),
        Index(
            "uq_sa_action_branch",
            "sa_id",
            "branch_id",
            "type",
            unique=True,
            postgresql_where=text("branch_id IS NOT NULL"),
        ),
        Index(
            "uq_sa_action_new_pair",
            "sa_id",
            "program_id",
            "product_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("type = 'NEW_BRANCH'"),
        ),
        Index(
            "uq_sa_action_contract",
            "sa_id",
            unique=True,
            postgresql_where=text("type = 'EXTEND_CONTRACT'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sa_id: Mapped[int] = mapped_column(
        ForeignKey("supplementary_agreements.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(20))
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="CASCADE"), nullable=True
    )
    program_id: Mapped[int | None] = mapped_column(
        ForeignKey("it_programs.id", ondelete="RESTRICT"), nullable=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    license_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # ветка, заведённая NEW_BRANCH при одобрении
    result_branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[created_at_dt]
