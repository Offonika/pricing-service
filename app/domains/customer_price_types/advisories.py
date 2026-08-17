"""Read-only customer advisories for the price-type shadow contour.

The objects in this module are drafts for a manager-facing surface.  They never
send messages, block an order, or authorize a write to 1C.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LampSeverity = Literal["none", "info", "warning", "critical", "review"]
NotificationEvent = Literal["presignal", "price_type_changed", "recovery"]


@dataclass(frozen=True, slots=True)
class OrderReturnLamp:
    key: str
    severity: LampSeverity
    title: str
    manager_action: str
    visible: bool
    blocks_fulfillment: bool = False


@dataclass(frozen=True, slots=True)
class CustomerNotificationDraft:
    event: NotificationEvent
    text: str
    channel_candidates: tuple[str, ...]
    approval_status: Literal["requires_approval"] = "requires_approval"
    send_allowed: bool = False


NO_RETURN_LAMP = OrderReturnLamp(
    key="no_return_signal",
    severity="none",
    title="",
    manager_action="",
    visible=False,
)


def build_order_return_lamp(
    *,
    character: str | None,
    period_mismatch: str | None = None,
    behavior_group: str | None = None,
) -> OrderReturnLamp:
    """Normalize the returns portrait into a non-blocking order-entry lamp."""

    normalized = (character or "").strip().lower()
    mismatch = (period_mismatch or "").strip().lower()
    behavior = (behavior_group or "").strip().lower()

    if "вне клиентского контура" in normalized:
        return NO_RETURN_LAMP
    if mismatch or "сверка периодов" in normalized or behavior == "needs_review_period_mismatch":
        return OrderReturnLamp(
            key="return_period_review",
            severity="review",
            title="Нужна сверка периода возвратов",
            manager_action="Уточнить, к какой продаже относится возврат, до вывода о характере клиента.",
            visible=True,
        )
    if "разовая сделка" in normalized:
        return OrderReturnLamp(
            key="one_off_return",
            severity="info",
            title="Новичок купил и вернул",
            manager_action="Проверить совместимость и ожидания клиента до сборки заказа.",
            visible=True,
        )
    if "подбор запчасти" in normalized:
        return OrderReturnLamp(
            key="parts_fitting_returns",
            severity="warning",
            title="Вероятен подбор запчасти",
            manager_action="Помочь проверить совместимость товара до сборки и отгрузки.",
            visible=True,
        )
    if "сверхнормативные возвраты" in normalized or "мозга-шпиль" in normalized:
        return OrderReturnLamp(
            key="critical_returns",
            severity="critical",
            title="Сверхнормативные возвраты",
            manager_action="До сборки проверить цель заказа и историю возвратов; санкции автоматически не применять.",
            visible=True,
        )
    if "повышенные возвраты" in normalized:
        return OrderReturnLamp(
            key="elevated_returns",
            severity="warning",
            title="Повышенный уровень возвратов",
            manager_action="Обсудить совместимость и условия возврата до сборки заказа.",
            visible=True,
        )
    return NO_RETURN_LAMP


def build_customer_notification_draft(
    event: NotificationEvent,
    *,
    current_level: str | None = None,
) -> CustomerNotificationDraft:
    """Build an unapproved message draft without choosing or calling a provider."""

    level = (current_level or "текущий уровень").strip()
    texts = {
        "presignal": (
            f"Напоминаем: чтобы сохранить условия «{level}», пожалуйста, доберите закупки "
            "до норматива текущего периода. Менеджер поможет проверить остаток до норматива."
        ),
        "price_type_changed": (
            "Условия закупки обновлены по итогам периода. Прежний уровень вернётся с "
            "возобновлением закупок по действующим правилам. Подробности можно уточнить у менеджера."
        ),
        "recovery": (
            "Давно не было закупок. Если работа продолжается, свяжитесь с менеджером — он поможет "
            "подобрать товар и восстановить подходящие условия по действующим правилам."
        ),
    }
    try:
        text = texts[event]
    except KeyError as exc:  # pragma: no cover - protects untyped runtime callers
        raise ValueError(f"unsupported notification event: {event}") from exc
    return CustomerNotificationDraft(
        event=event,
        text=text,
        channel_candidates=("sms", "whatsapp", "telegram", "manager_call"),
    )
