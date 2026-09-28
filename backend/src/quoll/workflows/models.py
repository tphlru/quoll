from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from quoll.core.mixins import IdMixin, TimestampMixin
from quoll.db import Base, created_at_dt, str_255

if TYPE_CHECKING:
    from quoll.attachments.models import Attachment


class Workflow(Base, IdMixin, TimestampMixin):
    name: Mapped[str_255] = mapped_column(unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # после публикации стадии менять нельзя - по ним уже едут заявки
    is_published: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    # за сколько дней предупреждать о конце лицензии и договора (Т-3)
    warn_days: Mapped[list[int]] = mapped_column(
        JSONB, default=lambda: [60, 30], server_default=text("'[60, 30]'::jsonb")
    )

    stages: Mapped[list["Stage"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="Stage.position",
    )
    transitions: Mapped[list["WorkflowTransition"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
    )


class Stage(Base, IdMixin, TimestampMixin):
    __table_args__ = (
        # дублирует первичный ключ, но нужна как цель составного ключа
        # из interactions - чтобы стадия не оказалась из чужого воркфлоу
        UniqueConstraint("id", "workflow_id", name="uq_stages_id_workflow_id"),
        CheckConstraint(
            "NOT is_branch_start OR is_branch_stage", name="chk_stage_branch_start"
        ),
        CheckConstraint(
            "stall_days IS NULL OR stall_days > 0", name="chk_stage_stall_days"
        ),
        CheckConstraint(
            "NOT is_parallel OR (NOT is_branch_stage AND NOT is_terminal "
            "AND NOT is_branch_start)",
            name="chk_stage_parallel",
        ),
        Index(
            "uq_stages_one_parallel",
            "workflow_id",
            unique=True,
            postgresql_where=text("is_parallel AND archived_at IS NULL"),
        ),
        CheckConstraint(
            "passive_after_days IS NULL OR (passive_after_days > 0 AND is_branch_stage)",
            name="chk_stage_passive_after_days",
        ),
        # после подписания ветки встают в одну стадию
        Index(
            "uq_stages_one_branch_start",
            "workflow_id",
            unique=True,
            postgresql_where=text("is_branch_start AND archived_at IS NULL"),
        ),
    )

    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str_255]
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # заявка закрыта, слот менеджера освобождается
    is_terminal: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    # стадию не удаляют, а архивируют: на неё ссылаются история и документы
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # стадия ветки продукта: после подписания шаги идут по каждому продукту
    is_branch_stage: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_branch_start: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # шаг вне цепочки (4.1 «Допсоглашение»): рёбер нет, на нём никто не стоит
    is_parallel: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # поля шага: [{key, label, type, required}] - заполняет менеджер,
    # обязательные проверяются при уходе со стадии вперёд
    fields: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )

    # порог застоя по умолчанию (Т-3); пусто - застой на шаге не считается
    stall_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # долгосрочный этап веток: столько дней без действий - и заявка уходит
    # в пассивные слоты (Д19)
    passive_after_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # подшаг x.1 шага x: его документ может заменить документ родителя
    parent_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="RESTRICT"), nullable=True
    )

    workflow: Mapped[Workflow] = relationship(back_populates="stages")


class TransitionAttachment(Base):
    transition_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_transitions.id", ondelete="CASCADE"), primary_key=True
    )
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class WorkflowTransition(Base, IdMixin, TimestampMixin):
    __table_args__ = (
        # петля из стадии в неё же бессмысленна и дала бы цикл блокировок
        # с архивацией - см. core/locking.py
        CheckConstraint(
            "from_stage_id IS DISTINCT FROM to_stage_id", name="chk_transition_no_loop"
        ),
        # начальная стадия - цель ребра из NULL, и она одна (П3)
        Index(
            "uq_transitions_one_start",
            "workflow_id",
            unique=True,
            postgresql_where=text("from_stage_id IS NULL AND is_active"),
        ),
        # в начальную стадию ставит принятие заявки - аппрува там нет
        CheckConstraint(
            "NOT (requires_approval AND from_stage_id IS NULL)",
            name="chk_transition_approval_not_on_entry",
        ),
        CheckConstraint(
            "reject_to_stage_id IS NULL OR requires_approval",
            name="chk_transition_reject_needs_approval",
        ),
        Index(
            "uq_transitions_active_edge",
            "workflow_id",
            "from_stage_id",
            "to_stage_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    from_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    to_stage_id: Mapped[int] = mapped_column(
        ForeignKey("stages.id", ondelete="CASCADE"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    name: Mapped[str_255]
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    # менеджер не проходит сам, а просит руководителя
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # куда уводит отказ в аппруве; без неё отказ оставляет на месте
    reject_to_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("stages.id", ondelete="SET NULL"), nullable=True
    )
    # возврат назад - только с комментарием
    is_backward: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # точка невозврата: откат руководителем не уходит раньше неё
    is_irreversible: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # какие типы актуальных документов нужны на стадии-источнике
    required_document_kinds: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )

    workflow: Mapped[Workflow] = relationship(back_populates="transitions")
    from_stage: Mapped["Stage | None"] = relationship(foreign_keys=[from_stage_id])
    to_stage: Mapped[Stage] = relationship(foreign_keys=[to_stage_id])
    attachments: Mapped[list["Attachment"]] = relationship(
        "Attachment",
        secondary="transition_attachments",
        lazy="selectin",
    )


class WorkflowChangeRequest(Base):
    """просьба руководителя изменить воркфлоу (Q11). Решает админ; саму
    правку он делает в редакторе графа - правки слишком разные, чтобы
    описывать их структурой ради одной кнопки"""

    __tablename__ = "workflow_change_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED')",
            name="chk_workflow_change_status",
        ),
        CheckConstraint(
            "(status = 'PENDING') = (decided_at IS NULL)",
            name="chk_workflow_change_decided",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default="PENDING", index=True
    )
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[created_at_dt]
