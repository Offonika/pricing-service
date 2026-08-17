"""Independent price-type and client-action review workflow.

The service persists the business decision and its external-action outbox entry in
one database transaction. It never calls Bitrix24 or 1C directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.domains.customer_price_types import CustomerPriceTypeAccessScope
from app.infrastructure.customer_price_types import SqlAlchemyCustomerPriceTypeRepository
from app.models.customer_price_type import (
    CustomerPriceTypeCaseEvent,
    CustomerPriceTypeExternalAction,
    CustomerPriceTypeOneCContractAction,
    CustomerPriceTypeReview,
)

PRICE_LADDER = (
    "Розница",
    "2.Бронзовый",
    "3.Серебряный",
    "4.Золотой",
    "5.Платиновый",
)
PRICE_DIRECTION_KEYS = {
    ("Розница", "2.Бронзовый"): "retail_to_bronze",
    ("2.Бронзовый", "Розница"): "bronze_to_retail",
    ("2.Бронзовый", "3.Серебряный"): "bronze_to_silver",
    ("3.Серебряный", "2.Бронзовый"): "silver_to_bronze",
    ("3.Серебряный", "4.Золотой"): "silver_to_gold",
    ("4.Золотой", "3.Серебряный"): "gold_to_silver",
    ("4.Золотой", "5.Платиновый"): "gold_to_platinum",
    ("5.Платиновый", "4.Золотой"): "platinum_to_gold",
}
CLIENT_ACTIONS = (
    "presignal",
    "retention",
    "isolate",
    "recovery",
    "quality",
    "credit",
    "economics",
)
CLIENT_ACTION_LABELS = {
    "presignal": "Предсигнал",
    "retention": "Удержание клиента",
    "isolate": "Изолятор на полный календарный месяц",
    "recovery": "Реанимация клиента",
    "quality": "Проверка качества",
    "credit": "Проверка кредита",
    "economics": "Проверка экономики",
}
_CASE_ACTIONS = {
    "manager_work": "retention",
    "isolate": "isolate",
    "recovery": "recovery",
}
_EXCLUDED_RECOMMENDATIONS = {
    "data_check",
    "insufficient_history",
    "new_client",
    "excluded_without_sales_history",
    "excluded_service_card",
}


class CustomerPriceTypeReviewConflict(RuntimeError):
    """The command is stale or the same dimension has already been decided."""


class CustomerPriceTypeReviewNotFound(LookupError):
    """The snapshot is absent or outside the caller scope."""


@dataclass(frozen=True, slots=True)
class ReviewSaveResult:
    card: dict[str, Any]
    saved_kind: str


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _client_action(snapshot: Any) -> str | None:
    if snapshot.case_type in _CASE_ACTIONS:
        return _CASE_ACTIONS[snapshot.case_type]
    if snapshot.review_type in {"quality", "credit", "economics"}:
        return str(snapshot.review_type)
    if snapshot.case_type == "special_review":
        return "quality"
    if snapshot.system_recommendation in CLIENT_ACTIONS:
        return str(snapshot.system_recommendation)
    return None


def _data_state(snapshot: Any) -> tuple[str, str]:
    if snapshot.case_type == "data_check" or snapshot.system_recommendation == "data_check":
        return "technical_review", "Данные проверяет техническая команда"
    if snapshot.system_recommendation == "insufficient_history":
        return "insufficient_history", "Недостаточно истории для решения"
    if snapshot.system_recommendation in {
        "new_client",
        "excluded_without_sales_history",
    }:
        return "new_client", "Новый клиент — оценка пока не требуется"
    return "ready", "Данные готовы"


def _price_corrections(current: str | None) -> list[str]:
    if current not in PRICE_LADDER:
        return []
    index = PRICE_LADDER.index(current)
    return [
        PRICE_LADDER[item] for item in range(max(0, index - 1), min(len(PRICE_LADDER), index + 2))
    ]


def _price_review_eligible(snapshot: Any) -> bool:
    return bool(
        snapshot.source_status == "ready"
        and snapshot.system_recommendation not in _EXCLUDED_RECOMMENDATIONS
        and snapshot.case_type in {"upgrade_approval", "downgrade_approval"}
        and snapshot.price_type_variant is None
        and snapshot.current_price_type in PRICE_LADDER
        and snapshot.recommended_price_type in PRICE_LADDER
        and snapshot.recommended_price_type != snapshot.current_price_type
        and abs(
            PRICE_LADDER.index(snapshot.recommended_price_type)
            - PRICE_LADDER.index(snapshot.current_price_type)
        )
        == 1
    )


def _client_action_eligible(snapshot: Any) -> bool:
    return bool(
        snapshot.source_status == "ready"
        and snapshot.system_recommendation not in _EXCLUDED_RECOMMENDATIONS
        and snapshot.action_required
        and _client_action(snapshot) is not None
    )


def _external_state(
    repository: SqlAlchemyCustomerPriceTypeRepository, review: Any
) -> tuple[str, str | None]:
    if review is None:
        return "not_created", None
    actions = repository.external_actions_for_review(review.id)
    if not actions:
        return "not_created", None
    action = actions[-1]
    messages = {
        "held": "Решение сохранено в проверочном режиме; внешнее действие не запущено.",
        "pending": "Внешнее действие ожидает обработки.",
        "preflight": "Выполняется предварительная проверка без записи.",
        "ready_to_apply": "Предварительная проверка пройдена; ожидается применение.",
        "applying": "Изменение передано на применение.",
        "applied": "Внешнее действие выполнено и сверено.",
        "cancelled": "Изменение отменено до начала применения.",
        "technical_review": "Требуется техническая сверка; автоматический повтор остановлен.",
    }
    return action.status, messages.get(
        action.status, "Текущее техническое состояние не расшифровано."
    )


class CustomerPriceTypeReviewService:
    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self.session = session
        self.repository = SqlAlchemyCustomerPriceTypeRepository(session)
        self.settings = settings or get_settings()

    def list_cards(
        self,
        *,
        snapshot_month: date | None,
        access: CustomerPriceTypeAccessScope,
        search: str | None,
        review_kind: str | None,
        pending_only: bool,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        run = self.repository.latest_run(snapshot_month)
        if run is None:
            return {
                "run_id": None,
                "snapshot_month": snapshot_month,
                "ruleset_version": None,
                "source_status": "missing",
                "total": 0,
                "limit": limit,
                "offset": offset,
                "payload": [],
            }
        rows = self.repository.list_review_cards(
            run_id=run.id,
            access=access,
            search=search,
        )
        cards = [self._card_payload(row, access) for row in rows]
        if review_kind and not search:
            cards = [
                card
                for card in cards
                if card[review_kind]["can_review"] or card[review_kind]["review_id"] is not None
            ]
        if pending_only:
            kinds = (review_kind,) if review_kind else ("price_type", "client_action")
            cards = [card for card in cards if any(card[kind]["can_review"] for kind in kinds)]
        total = len(cards)
        return {
            "run_id": run.id,
            "snapshot_month": run.snapshot_month,
            "ruleset_version": run.ruleset_version,
            "source_status": run.status,
            "total": total,
            "limit": limit,
            "offset": offset,
            "payload": cards[offset : offset + limit],
        }

    def get_card(
        self,
        *,
        snapshot_id: int,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, Any]:
        row = self.repository.get_review_card(snapshot_id=snapshot_id, access=access)
        if row is None:
            raise CustomerPriceTypeReviewNotFound("Карточка проверки не найдена.")
        return self._card_payload(row, access)

    def save(
        self,
        *,
        snapshot_id: int,
        review_kind: str,
        result: str,
        corrected_value: str | None,
        comment: str | None,
        expected_version: int,
        snapshot_hash: str,
        access: CustomerPriceTypeAccessScope,
    ) -> ReviewSaveResult:
        if access.role not in {"network_head", "internal", "executive"}:
            raise PermissionError("Решение может сохранить только руководитель сети.")
        row = self.repository.get_review_card(snapshot_id=snapshot_id, access=access)
        if row is None:
            raise CustomerPriceTypeReviewNotFound("Карточка проверки не найдена.")
        profile, snapshot, case, price_review, action_review = row
        internal_no_change_audit = bool(
            review_kind == "price_type"
            and access.role in {"internal", "executive"}
            and snapshot.id
            in self.repository.internal_no_change_audit_snapshot_ids(
                run_id=snapshot.run_id, limit=30
            )
        )
        if access.role != "network_head" and not internal_no_change_audit:
            raise PermissionError("Решение может сохранить только руководитель сети.")
        existing = price_review if review_kind == "price_type" else action_review
        other = action_review if review_kind == "price_type" else price_review
        if existing is not None or expected_version != 0:
            raise CustomerPriceTypeReviewConflict(
                "Решение уже сохранено или версия карточки устарела. Обновите карточку."
            )
        if snapshot.snapshot_hash != snapshot_hash:
            raise CustomerPriceTypeReviewConflict(
                "Расчёт изменился. Обновите карточку и проверьте новое решение."
            )
        if other is not None and other.result == "data_issue" and result != "data_issue":
            raise ValueError(
                "Карточка заблокирована из-за ошибки в данных. Сначала нужна техническая сверка."
            )
        normalized_comment = comment.strip() if comment and comment.strip() else None
        if review_kind == "price_type":
            if not (_price_review_eligible(snapshot) or internal_no_change_audit):
                raise ValueError("Эта карточка не содержит типа цены для проверки.")
            final_value = self._validate_price_decision(
                snapshot=snapshot,
                result=result,
                corrected_value=corrected_value,
                comment=normalized_comment,
            )
            system_value = snapshot.recommended_price_type
        elif review_kind == "client_action":
            final_value = self._validate_action_decision(
                snapshot=snapshot,
                result=result,
                corrected_value=corrected_value,
                comment=normalized_comment,
            )
            system_value = _client_action(snapshot)
        else:
            raise ValueError("Неизвестный вид решения.")

        now = _utcnow()
        execution_allowed = self._execution_allowed(
            review_kind=review_kind,
            snapshot=snapshot,
            result=result,
            final_value=final_value,
            access=access,
        )
        review = CustomerPriceTypeReview(
            snapshot_id=snapshot.id,
            profile_id=profile.id,
            case_id=case.id if case else None,
            review_kind=review_kind,
            system_value=system_value,
            final_value=final_value,
            result=result,
            comment=normalized_comment,
            snapshot_hash=snapshot.snapshot_hash,
            decision_mode="live" if execution_allowed else "test",
            reviewed_by=access.actor,
            reviewed_at=now,
            version=1,
        )
        self.session.add(review)
        try:
            self.session.flush()
            external_action = None
            if not internal_no_change_audit:
                external_action = self._create_external_action(
                    review=review,
                    profile=profile,
                    snapshot=snapshot,
                    case=case,
                    result=result,
                    final_value=final_value,
                    execution_allowed=execution_allowed,
                )
            if result == "data_issue":
                self._block_external_actions(snapshot_id=snapshot.id, now=now)
            if case is not None:
                self.session.add(
                    CustomerPriceTypeCaseEvent(
                        case_id=case.id,
                        event_type=f"{review_kind}_reviewed",
                        actor=access.actor,
                        source="app",
                        before_status=case.stage,
                        after_status=case.stage,
                        comment=normalized_comment,
                        metadata_json={
                            "review_id": review.id,
                            "result": result,
                            "final_value": final_value,
                            "snapshot_hash": snapshot.snapshot_hash,
                            "external_action_id": external_action.id if external_action else None,
                            "execution_allowed_at_decision": execution_allowed,
                        },
                        idempotency_key=f"review:{review.id}:saved",
                    )
                )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise CustomerPriceTypeReviewConflict(
                "Решение уже сохранено другим пользователем. Обновите карточку."
            ) from exc
        refreshed = self.repository.get_review_card(snapshot_id=snapshot.id, access=access)
        if refreshed is None:  # pragma: no cover - guarded by the same transaction
            raise CustomerPriceTypeReviewNotFound("Карточка проверки не найдена.")
        return ReviewSaveResult(
            card=self._card_payload(refreshed, access),
            saved_kind=review_kind,
        )

    def cancel_change(
        self,
        *,
        case_id: int,
        expected_version: int,
        comment: str,
        access: CustomerPriceTypeAccessScope,
    ) -> CustomerPriceTypeExternalAction:
        if access.role not in {"network_head", "executive"}:
            raise PermissionError("Недостаточно прав для отмены изменения.")
        scoped_case = self.repository.get_case_scoped(case_id, access)
        if scoped_case is None:
            raise CustomerPriceTypeReviewNotFound("Рабочий кейс не найден.")
        action = self.session.scalar(
            select(CustomerPriceTypeExternalAction)
            .where(
                CustomerPriceTypeExternalAction.case_id == case_id,
                CustomerPriceTypeExternalAction.action_kind == "onec_change",
            )
            .order_by(CustomerPriceTypeExternalAction.id.desc())
        )
        if action is None:
            raise CustomerPriceTypeReviewNotFound(
                "Для кейса нет изменения, которое можно отменить."
            )
        if action.status in {"applying", "applied"}:
            raise CustomerPriceTypeReviewConflict(
                "Применение уже началось. Нужна техническая сверка и отдельное обратное решение."
            )
        if action.status == "cancelled":
            raise CustomerPriceTypeReviewConflict("Изменение уже отменено.")
        if action.status == "technical_review":
            raise CustomerPriceTypeReviewConflict("Карточка уже передана на техническую сверку.")
        now = _utcnow()
        result = self.session.execute(
            update(CustomerPriceTypeExternalAction)
            .where(
                CustomerPriceTypeExternalAction.id == action.id,
                CustomerPriceTypeExternalAction.version == expected_version,
                CustomerPriceTypeExternalAction.status.in_(
                    ("held", "pending", "preflight", "ready_to_apply")
                ),
            )
            .values(
                status="cancelled",
                cancelled_by=access.actor,
                cancelled_at=now,
                cancel_comment=comment.strip(),
                version=CustomerPriceTypeExternalAction.version + 1,
                updated_at=now,
            )
        )
        if not result.rowcount:
            self.session.rollback()
            raise CustomerPriceTypeReviewConflict(
                "Состояние изменения изменилось. Обновите карточку перед отменой."
            )
        self.session.execute(
            update(CustomerPriceTypeOneCContractAction)
            .where(CustomerPriceTypeOneCContractAction.external_action_id == action.id)
            .values(status="cancelled", updated_at=now)
        )
        case = scoped_case[0]
        self.session.add(
            CustomerPriceTypeCaseEvent(
                case_id=case.id,
                event_type="onec_change_cancelled",
                actor=access.actor,
                source="app",
                before_status=case.stage,
                after_status=case.stage,
                comment=comment.strip(),
                metadata_json={"external_action_id": action.id},
                idempotency_key=f"external-action:{action.id}:cancelled",
            )
        )
        self.session.commit()
        return self.session.get(CustomerPriceTypeExternalAction, action.id)

    def metrics(
        self,
        *,
        snapshot_month: date | None,
        access: CustomerPriceTypeAccessScope,
    ) -> dict[str, Any]:
        if access.role not in {"network_head", "executive", "internal", "quality"}:
            raise PermissionError("Недостаточно прав для просмотра показателей.")
        run = self.repository.latest_run(snapshot_month)
        if run is None:
            envelope = {
                "run_id": None,
                "snapshot_month": snapshot_month,
                "ruleset_version": None,
                "source_status": "missing",
            }
            empty = self.repository.review_metrics(run_id=-1, review_kind="price_type")
            return {**envelope, "price_type": empty, "client_action": empty}
        return {
            "run_id": run.id,
            "snapshot_month": run.snapshot_month,
            "ruleset_version": run.ruleset_version,
            "source_status": run.status,
            "price_type": self.repository.review_metrics(run_id=run.id, review_kind="price_type"),
            "client_action": self.repository.review_metrics(
                run_id=run.id, review_kind="client_action"
            ),
        }

    def _card_payload(
        self, row: tuple[Any, ...], access: CustomerPriceTypeAccessScope
    ) -> dict[str, Any]:
        profile, snapshot, case, price_review, action_review = row
        data_state, data_label = _data_state(snapshot)
        review_data_issue = bool(
            (price_review is not None and price_review.result == "data_issue")
            or (action_review is not None and action_review.result == "data_issue")
        )
        if review_data_issue:
            data_state = "technical_review"
            data_label = "Данные проверяет техническая команда"
        is_internal_audit = access.role in {
            "internal",
            "executive",
        } and snapshot.id in self.repository.internal_no_change_audit_snapshot_ids(
            run_id=snapshot.run_id, limit=30
        )
        price_eligible = (
            _price_review_eligible(snapshot) or is_internal_audit
        ) and not review_data_issue
        action = _client_action(snapshot)
        action_eligible = _client_action_eligible(snapshot) and not review_data_issue
        can_decide_price = access.role == "network_head" or is_internal_audit
        can_decide_action = access.role == "network_head"
        return {
            "snapshot_id": snapshot.id,
            "case_id": case.id if case else None,
            "counterparty_ref": profile.counterparty_ref,
            "counterparty_code": profile.counterparty_code,
            "counterparty_name": profile.counterparty_name,
            "owner_name": profile.owner_name,
            "department_name": profile.department_name,
            "snapshot_month": snapshot.snapshot_month,
            "current_price_type": snapshot.current_price_type,
            "recommended_price_type": (
                snapshot.recommended_price_type if data_state == "ready" else None
            ),
            "recommendation_text": snapshot.recommendation_reason,
            "data_state": data_state,
            "data_state_label": data_label,
            "snapshot_hash": snapshot.snapshot_hash,
            "contracts": [dict(item) for item in snapshot.contract_candidates],
            "price_type": self._dimension(
                repository=self.repository,
                kind="price_type",
                review=price_review,
                system_value=(snapshot.recommended_price_type if price_eligible else None),
                system_label=(
                    f"Изменить на {snapshot.recommended_price_type}"
                    if _price_review_eligible(snapshot)
                    else (
                        "Изменение не требуется — контроль расчёта"
                        if is_internal_audit
                        else "Изменение типа цены не требует согласования"
                    )
                ),
                eligible=price_eligible,
                can_decide=can_decide_price,
                unavailable_reason=self._price_unavailable_reason(snapshot),
                allowed_results=["confirm", "correct", "data_issue"],
                corrected_values=_price_corrections(snapshot.current_price_type),
            ),
            "client_action": self._dimension(
                repository=self.repository,
                kind="client_action",
                review=action_review,
                system_value=action,
                system_label=(
                    CLIENT_ACTION_LABELS.get(action, "Действие не требуется")
                    if action_eligible
                    else "Содержательное действие не требуется"
                ),
                eligible=action_eligible,
                can_decide=can_decide_action,
                unavailable_reason=self._action_unavailable_reason(snapshot),
                allowed_results=["confirm", "correct", "no_action", "data_issue"],
                corrected_values=list(CLIENT_ACTIONS),
            ),
        }

    @staticmethod
    def _dimension(
        *,
        repository: SqlAlchemyCustomerPriceTypeRepository,
        kind: str,
        review: Any,
        system_value: str | None,
        system_label: str,
        eligible: bool,
        can_decide: bool,
        unavailable_reason: str | None,
        allowed_results: list[str],
        corrected_values: list[str],
    ) -> dict[str, Any]:
        external_state, external_message = _external_state(repository, review)
        return {
            "kind": kind,
            "system_value": system_value,
            "system_label": system_label,
            "can_review": bool(eligible and can_decide and review is None),
            "unavailable_reason": unavailable_reason if not eligible else None,
            "allowed_results": allowed_results if eligible and review is None else [],
            "allowed_corrected_values": corrected_values if eligible and review is None else [],
            "review_id": review.id if review else None,
            "result": review.result if review else None,
            "final_value": review.final_value if review else None,
            "comment": review.comment if review else None,
            "reviewed_by": review.reviewed_by if review else None,
            "reviewed_at": review.reviewed_at if review else None,
            "version": review.version if review else 0,
            "decision_mode": review.decision_mode if review else None,
            "external_state": external_state,
            "external_message": external_message,
        }

    @staticmethod
    def _price_unavailable_reason(snapshot: Any) -> str:
        state, label = _data_state(snapshot)
        if state != "ready":
            return label
        if snapshot.recommended_price_type == snapshot.current_price_type:
            return "Текущий и рекомендуемый тип совпадают; подтверждение не требуется."
        if snapshot.case_type in {"manager_work", "isolate", "recovery"}:
            return "Сначала нужно завершить действие с клиентом и выполнить новый расчёт."
        if snapshot.price_type_variant or snapshot.current_price_type not in PRICE_LADDER:
            return "Специальный тип цены нельзя согласовать через эту карточку."
        return "Готового изменения типа цены пока нет."

    @staticmethod
    def _action_unavailable_reason(snapshot: Any) -> str:
        state, label = _data_state(snapshot)
        if state != "ready":
            return label
        return "Содержательное действие с клиентом не сформировано."

    @staticmethod
    def _validate_price_decision(
        *, snapshot: Any, result: str, corrected_value: str | None, comment: str | None
    ) -> str | None:
        if not _price_review_eligible(snapshot) and not (
            snapshot.source_status == "ready"
            and snapshot.current_price_type in PRICE_LADDER
            and snapshot.recommended_price_type == snapshot.current_price_type
        ):
            raise ValueError("Эта карточка не содержит готового изменения типа цены.")
        if result == "confirm":
            if corrected_value is not None:
                raise ValueError("Для подтверждения исправленный тип не указывается.")
            return snapshot.recommended_price_type
        if result == "correct":
            if comment is None:
                raise ValueError("Для исправления типа цены обязателен комментарий.")
            if corrected_value not in _price_corrections(snapshot.current_price_type):
                raise ValueError(
                    "Можно выбрать текущий тип или только соседний стандартный уровень."
                )
            return corrected_value
        if result == "data_issue":
            if comment is None:
                raise ValueError("Для ошибки в данных обязателен комментарий.")
            return None
        raise ValueError("Для типа цены выбрано недопустимое решение.")

    @staticmethod
    def _validate_action_decision(
        *, snapshot: Any, result: str, corrected_value: str | None, comment: str | None
    ) -> str | None:
        action = _client_action(snapshot)
        if not _client_action_eligible(snapshot) or action is None:
            raise ValueError("Эта карточка не содержит действия для согласования.")
        if result == "confirm":
            if corrected_value is not None:
                raise ValueError("Для подтверждения исправленное действие не указывается.")
            return action
        if result == "correct":
            if comment is None or corrected_value not in CLIENT_ACTIONS:
                raise ValueError(
                    "Для исправления выберите правильное действие и добавьте комментарий."
                )
            return corrected_value
        if result == "no_action":
            if corrected_value is not None:
                raise ValueError("Для решения «Действие не требуется» значение не указывается.")
            return None
        if result == "data_issue":
            if comment is None:
                raise ValueError("Для ошибки в данных обязателен комментарий.")
            return None
        raise ValueError("Для действия с клиентом выбрано недопустимое решение.")

    def _execution_allowed(
        self,
        *,
        review_kind: str,
        snapshot: Any,
        result: str,
        final_value: str | None,
        access: CustomerPriceTypeAccessScope,
    ) -> bool:
        if access.role != "network_head":
            return False
        if not self.settings.customer_price_type_external_actions_enabled:
            return False
        if result in {"data_issue", "no_action"} or final_value is None:
            return False
        if review_kind == "client_action":
            return self.settings.customer_price_type_bitrix_case_actions_enabled
        if final_value == snapshot.current_price_type:
            return False
        direction = PRICE_DIRECTION_KEYS.get((snapshot.current_price_type, final_value))
        return bool(
            direction
            and self.settings.customer_price_type_onec_actions_enabled
            and direction in set(self.settings.customer_price_type_onec_enabled_directions)
        )

    def _create_external_action(
        self,
        *,
        review: CustomerPriceTypeReview,
        profile: Any,
        snapshot: Any,
        case: Any,
        result: str,
        final_value: str | None,
        execution_allowed: bool,
    ) -> CustomerPriceTypeExternalAction | None:
        if result in {"data_issue", "no_action"} or final_value is None:
            return None
        if review.review_kind == "price_type" and final_value == snapshot.current_price_type:
            return None
        action_kind = "onec_change" if review.review_kind == "price_type" else "bitrix_case"
        action = CustomerPriceTypeExternalAction(
            review_id=review.id,
            snapshot_id=snapshot.id,
            case_id=case.id if case else None,
            action_kind=action_kind,
            idempotency_key=f"customer-price-type-review:{review.id}:{action_kind}",
            status="pending" if execution_allowed else "held",
            execution_allowed_at_decision=execution_allowed,
            snapshot_hash=snapshot.snapshot_hash,
            payload={
                "counterparty_ref": profile.counterparty_ref,
                "snapshot_month": snapshot.snapshot_month.isoformat(),
                "review_kind": review.review_kind,
                "final_value": final_value,
            },
            version=1,
        )
        self.session.add(action)
        self.session.flush()
        if action_kind == "onec_change":
            candidates = [
                item
                for item in snapshot.contract_candidates
                if item.get("is_working") and item.get("used_for_calculation")
            ]
            if not candidates:
                raise ValueError("Не найден точный рабочий договор для изменения в 1С.")
            for item in candidates:
                contract_ref = str(item.get("contract_ref") or "").strip()
                if not contract_ref:
                    raise ValueError("У рабочего договора отсутствует идентификатор 1С.")
                self.session.add(
                    CustomerPriceTypeOneCContractAction(
                        external_action_id=action.id,
                        idempotency_key=f"{action.idempotency_key}:{contract_ref.lower()}",
                        contract_ref=contract_ref.lower(),
                        contract_name=item.get("contract_name"),
                        expected_price_type=snapshot.current_price_type,
                        target_price_type=final_value,
                        status="pending" if execution_allowed else "held",
                    )
                )
        return action

    def _block_external_actions(self, *, snapshot_id: int, now: datetime) -> None:
        actions = list(
            self.session.scalars(
                select(CustomerPriceTypeExternalAction).where(
                    CustomerPriceTypeExternalAction.snapshot_id == snapshot_id,
                    CustomerPriceTypeExternalAction.status.not_in(
                        ("applied", "cancelled", "technical_review")
                    ),
                )
            )
        )
        for action in actions:
            if action.status == "applying":
                action.status = "technical_review"
                action.technical_message = (
                    "Во время применения обнаружена ошибка в данных; требуется ручная сверка."
                )
                line_status = "technical_review"
            else:
                action.status = "cancelled"
                action.cancelled_by = "system:data_issue"
                action.cancelled_at = now
                action.cancel_comment = "В карточке зафиксирована ошибка в данных."
                line_status = "cancelled"
            action.version += 1
            action.updated_at = now
            self.session.execute(
                update(CustomerPriceTypeOneCContractAction)
                .where(CustomerPriceTypeOneCContractAction.external_action_id == action.id)
                .values(status=line_status, updated_at=now)
            )
