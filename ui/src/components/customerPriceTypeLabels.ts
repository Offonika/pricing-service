const ROLE_LABELS: Record<string, string> = {
  internal: "Внутренний администратор",
  executive: "Генеральный директор",
  manager: "Менеджер",
  network_head: "Руководитель сети",
  department_head: "Руководитель подразделения",
  master_data: "Специалист по данным",
  quality: "Специалист по качеству",
  finance: "Финансовый специалист",
  integration_operator: "Оператор интеграции",
};

const STATUS_LABELS: Record<string, string> = {
  started: "Запущен",
  completed: "Завершён",
  partial: "Данные загружены частично",
  failed: "Ошибка",
  missing: "Нет данных",
  ready: "Готово",
  conflict: "Конфликт данных",
  excluded: "Исключён",
  not_requested: "Не запрашивалось",
  pending: "Ожидает согласования",
  approved: "Согласовано",
  rejected: "Отклонено",
  stale: "Требует повторной проверки",
  NEW: "Новый",
};

const RECOMMENDATION_LABELS: Record<string, string> = {
  excluded_service_card: "Исключить служебную карточку",
  insufficient_history: "Недостаточно истории",
  new_client: "Новый клиент",
  informational_upgrade_candidate: "Кандидат на повышение",
  keep_current: "Оставить текущий тип цены",
  special_review: "Провести специальную проверку",
  downgrade_to_retail: "Перевести на розничный тип цены",
  recovery: "Вернуть клиента в работу",
  manager_retention: "Передать менеджеру на удержание",
  isolate: "Перевести в изолятор",
  data_check: "Сверить данные",
};

const FACTOR_LABELS: Record<string, string> = {
  service_card: "Служебная карточка",
  manual_override: "Ручное решение",
  source_conflict: "Конфликт источников",
  insufficient_history: "Недостаточно истории",
  new_client: "Новый клиент",
  upgrade_freeze: "Повышения временно приостановлены",
  returns_advisory_only: "Возвраты требуют ручной оценки",
  key_account_no_numeric_threshold: "Для ключевого клиента нет числового порога",
  key_account: "Ключевой клиент",
  economics_required: "Нужна проверка экономики",
  human_approval_required: "Требуется решение ответственного",
  isolation_required: "Требуется период изолятора",
  active_contract_missing: "Нет активного договора",
  multi_contract: "Найдено несколько договоров",
  price_type_missing: "В договоре не задан тип цены",
  price_type_marked: "Тип цены помечен на удаление",
  unknown_price_type: "Тип цены не распознан",
  duplicate_counterparty: "Обнаружен дубль клиента",
  partial_source: "Источник загружен частично",
  source_mismatch: "Источники расходятся",
  return_period_mismatch: "Не совпадает период возвратов",
  economics_missing: "Нет экономических данных",
};

const EVENT_LABELS: Record<string, string> = {
  profile_excluded: "Профиль исключён",
  case_created: "Кейс создан",
  snapshot_changed: "Расчётные данные изменились",
};

const SOURCE_LABELS: Record<string, string> = {
  contracts: "договоры",
  sales: "продажи",
  payments: "оплаты",
  returns: "возвраты",
  economics: "экономика",
  master_data: "основные данные",
};

function knownLabel(value: string | null | undefined, labels: Record<string, string>): string {
  if (!value) return "—";
  return labels[value] ?? "Неизвестное значение";
}

export function roleLabel(value: string | null | undefined): string {
  return knownLabel(value, ROLE_LABELS);
}

export function statusLabel(value: string | null | undefined): string {
  return knownLabel(value, STATUS_LABELS);
}

export function recommendationLabel(value: string | null | undefined): string {
  if (!value) return "—";
  if (value.startsWith("manual_override:")) return "Ручное решение";
  return knownLabel(value, RECOMMENDATION_LABELS);
}

export function factorLabel(value: string): string {
  const exact = FACTOR_LABELS[value];
  if (exact) return exact;

  const sourceMatch = /^source_([^_]+(?:_[^_]+)*)_(missing|partial|conflict|failed)$/.exec(value);
  if (sourceMatch) {
    const source = SOURCE_LABELS[sourceMatch[1]] ?? "данные";
    const state = statusLabel(sourceMatch[2]);
    return `Источник «${source}»: ${state.toLocaleLowerCase("ru-RU")}`;
  }
  return "Неизвестный ограничивающий фактор";
}

export function eventLabel(value: string): string {
  return knownLabel(value, EVENT_LABELS);
}

export function reasonLabel(value: string): string {
  const match = /^Требуется сверка данных: ([a-z0-9_]+)\.$/.exec(value);
  if (match) return `Требуется сверка данных: ${factorLabel(match[1]).toLocaleLowerCase("ru-RU")}.`;
  return value
    .replaceAll("Key Account", "ключевой клиент")
    .replaceAll("B2B-квалификацию", "корпоративную квалификацию")
    .replaceAll("CRM-реанимации", "возврата в работу")
    .replaceAll("в v1", "в первой версии");
}

export function snapshotMonthLabel(value: string | null | undefined): string {
  if (!value || !/^\d{4}-\d{2}$/.test(value)) return "—";
  const [year, month] = value.split("-").map(Number);
  return new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(Date.UTC(year, month - 1, 1)),
  );
}
