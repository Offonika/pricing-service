// Сервер отвечает техническим текстом на английском. Держим переводы в одном
// месте: до этого словарь жил только в карточке заказа, а на витрине и в
// помощнике пользователь видел, например, «classification proposal cannot be
// self-approved» без объяснения, что делать дальше.
const PROCUREMENT_ERROR_MESSAGES: Record<string, string> = {
  "order version changed; refresh the order":
    "Заказ уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.",
  "order line version changed; refresh the order":
    "Строку уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.",
  "transmitted order is read-only; create a new version":
    "Заказ уже передан в 1С, его нельзя менять. Создайте новую версию заказа.",
  "approved order is read-only; create a new revision":
    "Подтверждённый заказ заморожен. Новый расчёт создаст отдельную ревизию.",
  "classification proposal cannot be self-approved":
    "Своё предложение согласовать нельзя — решение принимает второй сотрудник закупки.",
  "user cannot approve product classification":
    "У вас нет прав согласовывать классификацию товара.",
  "only proposed classification can be approved":
    "Решение по этому предложению уже принято — обновите список.",
  "classification proposal was not found":
    "Предложение не найдено: возможно, его уже обработали в другом окне.",
  "classification reason is required": "Укажите причину изменения классификации.",
  "review date is required when manual minimum is set":
    "При ручном минимуме обязательно укажите дату пересмотра.",
  "manual minimum cannot be negative": "Ручной минимум не может быть отрицательным.",
  "classification approver user IDs are not configured":
    "Не настроен список сотрудников, которые согласовывают классификацию.",
  "selected supplier was not found in 1C":
    "Поставщик больше не найден в 1С — обновите поиск и выберите его заново.",
  "main supplier changed in 1C; refresh the order and use the 1C value":
    "Основного поставщика уже изменили в 1С. Обновите заказ: значение 1С имеет приоритет.",
  "only the supplier review room can be distributed":
    "Разнести строки можно только из комнаты разбора без поставщика.",
  "no lines with a selected supplier to distribute":
    "Сначала назначьте поставщика хотя бы одной строке.",
};

export function procurementErrorText(error: unknown, fallback = "Операция не выполнена") {
  const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
  if (detail) return PROCUREMENT_ERROR_MESSAGES[detail] || detail;
  if (error instanceof Error) return PROCUREMENT_ERROR_MESSAGES[error.message] || error.message;
  return fallback;
}
