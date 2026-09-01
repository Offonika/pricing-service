import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import toast from "react-hot-toast";
import {
  applyProcurementSupplierDistribution,
  approveProcurementClassification,
  createProcurementClassification,
  downloadProcurementOrderLabels,
  fetchProcurementOrderLabelPreview,
  fetchProcurementOrder,
  linkProcurementOrderLabelSource,
  previewProcurementSupplierDistribution,
  searchProcurementSupplierOptions,
  selectProcurementLineMainSupplier,
  submitProcurementOrder,
  updateProcurementOrderLine,
  type ProcurementSupplierDistributionPreview,
  type ProcurementSupplierOption,
  type ProcurementOrderFormation,
  type ProcurementOrderLabelPreview,
  type ProcurementOrderFormationLine,
  type ProcurementBlockerDetail,
} from "../api/procurementAssortment";
import { openBitrixProcurementProcess } from "../api/bitrix";
import { procurementErrorText } from "../utils/procurementErrorMessages";
import {
  groupProcurementBlockers,
  procurementBlockerText,
  procurementRiskLabel,
} from "../utils/procurementRiskLabels";

interface Props {
  bitrixUserName?: string | null;
  focusLineId?: number;
  initialOrder: ProcurementOrderFormation;
  onBack?: () => void;
}

interface LineEdit {
  quantity: string;
  price: string;
}

interface ClassificationEdit {
  status: string;
  reason: string;
  manualMinimum: string;
  reviewDate: string;
  replacementSkuCode: string;
  noReplacement: boolean;
}

// Статусы, снимающие карточку с ведения: для них нужен код карточки-победителя
// семьи либо явная отметка «замены нет» (решение 2026-08-18).
const REPLACEMENT_REQUIRED_STATUSES = new Set(["pension", "replace_candidate", "do_not_order"]);

const ORDER_STATUS_LABELS: Record<string, string> = {
  draft: "Заказ на подтверждении",
  review: "На проверке",
  approved: "Согласовано к 1С",
  transmitting: "Передача в 1С",
  transmitted: "Передано в 1С",
  deferred: "Отложено / отменено",
  error: "Ошибка передачи",
};

const ONEC_STATUS_LABELS: Record<string, string> = {
  not_sent: "Не отправлен",
  pending: "Ожидает передачи",
  sent: "Отправлен",
  accepted: "Принят в 1С",
  error: "Ошибка передачи",
};

const ROUTE_LABELS: Record<string, string> = {
  ordinary: "Обычная закупка",
  urgent: "Срочная закупка",
  direct: "Прямая поставка",
};

function money(value: string, currency: string) {
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(number);
}

function numeric(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function percent(value: unknown) {
  const parsed = numeric(value);
  return parsed === null
    ? "нет данных"
    : `${parsed.toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

function quantity(value: unknown) {
  const parsed = numeric(value);
  return parsed === null
    ? String(value ?? "—")
    : parsed.toLocaleString("ru-RU", { maximumFractionDigits: 3 });
}

function inputNumber(value: string) {
  const parsed = numeric(value);
  return parsed === null ? value : String(parsed);
}

function countLabel(count: number, one: string, few: string, many: string) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return `${count} ${many}`;
  if (last === 1) return `${count} ${one}`;
  if (last >= 2 && last <= 4) return `${count} ${few}`;
  return `${count} ${many}`;
}

function returnLabel(value: unknown) {
  const count = numeric(value);
  return count === null
    ? "количество возвратов не определено"
    : countLabel(count, "возврат", "возврата", "возвратов");
}

function batchName(value: unknown) {
  return typeof value === "string" && value.trim()
    ? value.trim().replace(/^партия\s+/i, "")
    : "не определена";
}

function blockerDetailMessage(detail: ProcurementBlockerDetail) {
  if (detail.code !== "batch_error_suspected") return detail.message;
  const evidence = detail.evidence;
  const share = numeric(evidence.share_pct);
  const windowDays = numeric(evidence.window_days);
  const minimumReturns = numeric(evidence.minimum_return_qty);
  const minimumShare = numeric(evidence.minimum_share_pct);
  const batch = batchName(evidence.suspected_batch);
  return `Подозрение на партийную ошибку: ${returnLabel(evidence.return_qty)}, ${percent(share)}${windowDays === null ? "" : ` за ${windowDays} дней`} (порог: ${minimumReturns === null ? "—" : returnLabel(minimumReturns)} и ${percent(minimumShare)}). Партия: ${batch}.`;
}

function blockerSummaryMessage(detail: ProcurementBlockerDetail) {
  if (detail.code === "batch_error_suspected") return "Подозрение на партийную ошибку";
  if (detail.code === "defect_rate_suspected") return "Подтверждённый высокий процент брака";
  if (detail.code === "supplier_defect_over_10_pct_reliable") return "Брак поставщика выше порога";
  if (detail.code === "purchase_price_change_over_10_pct") return "Изменение закупочной цены требует проверки";
  const message = detail.message.trim();
  const technicalMessage = detail.code.replaceAll("_", " ");
  if (!message || message === detail.code || message.toLocaleLowerCase() === technicalMessage) {
    return procurementRiskLabel(detail.code);
  }
  return message;
}

function profitabilityText(line: ProcurementOrderFormationLine) {
  const value = numeric(line.profitability_pct);
  if (value !== null) return percent(value);
  if (line.profitability_status && line.profitability_status !== "ready") {
    return `не рассчитана: ${line.profitability_explanation || "нет данных за период"}`;
  }
  return "нет данных";
}

function payloadValue(line: ProcurementOrderFormationLine, key: string) {
  return line.payload?.[key];
}

function lineProblemTexts(line: ProcurementOrderFormationLine, batchId: string) {
  if (line.blocker_details?.length) {
    const messages = line.blocker_details.map(blockerDetailMessage);
    if (line.removed) messages.unshift("Потребность исчезла в новом расчёте.");
    return [...new Set(messages)];
  }
  const values = line.blockers.map((code) => {
    if (code === "batch_error_suspected") {
      const returned = payloadValue(line, "batch_error_return_qty") || "?";
      const share = percent(payloadValue(line, "batch_error_share_pct"));
      return `Подозрение на пересорт: ${returnLabel(returned)} (${share} продаж). Точная партия поставки в источнике не определена; расчёт ${batchId}.`;
    }
    if (code === "defect_rate_suspected") {
      const returned = payloadValue(line, "defect_return_qty") || "?";
      const share = percent(payloadValue(line, "defect_share_pct"));
      return `Высокий процент брака: ${share} (${returnLabel(returned)}); автозаказ остановлен.`;
    }
    if (code === "supplier_defect_over_10_pct_reliable") {
      const basis = line.supplier_defect_history_units
        ? ` на базе ${line.supplier_defect_history_units.toLocaleString("ru-RU")} шт.`
        : "";
      return `У выбранного поставщика подтверждённый брак ${percent(line.supplier_defect_pct)}${basis}`;
    }
    if (code === "purchase_price_change_over_10_pct") {
      return `Закупочная цена изменилась на ${percent(line.price_change_pct)} — нужна проверка.`;
    }
    return procurementRiskLabel(code);
  });
  if (line.removed) values.unshift("Потребность исчезла в новом расчёте.");
  return [...new Set(values)];
}

function familyQuantityChanged(line: ProcurementOrderFormationLine) {
  const recommendation = line.display_family_recommendation;
  if (!recommendation) return false;
  const baseline = numeric(recommendation.baseline_order_qty);
  const allocated = numeric(recommendation.allocated_order_qty);
  return baseline !== null && allocated !== null && baseline !== allocated;
}

function visibleRecommendationReason(line: ProcurementOrderFormationLine) {
  if (!line.recommendation_reason) return null;
  const family = line.display_family_recommendation;
  if (family && line.recommendation_reason === family.reason_ru && !familyQuantityChanged(line)) {
    return null;
  }
  return line.recommendation_reason;
}

function roundingExplanation(line: ProcurementOrderFormationLine) {
  const family = line.display_family_recommendation;
  if (family && familyQuantityChanged(line)) {
    return `Семейное перераспределение: базово ${quantity(family.baseline_order_qty)} шт., итог ${quantity(family.allocated_order_qty)} шт.; после распределения применяется целое количество, а не кратность SKU.`;
  }
  const raw = payloadValue(line, "recommended_order_qty_raw");
  const multiple = payloadValue(line, "order_rounding_multiple");
  const gate = String(payloadValue(line, "order_rounding_price_gate") || "");
  const gateText = String(payloadValue(line, "order_rounding_price_gate_ru") || "");
  const median = payloadValue(line, "order_rounding_group_median_price");
  if (gate === "above_median") {
    return `Округление не применено: цена карточки выше медианы группы${median ? ` (${median})` : ""}.`;
  }
  if (gate === "no_purchase_price") {
    return "Округление не применено: нет подтверждённой закупочной цены.";
  }
  if (raw && multiple && numeric(raw) !== numeric(line.recommended_quantity)) {
    return `Округление: ${quantity(raw)} → ${quantity(line.recommended_quantity)} шт., кратность ${quantity(multiple)}.`;
  }
  if (gateText) return `Округление: ${gateText}.`;
  return null;
}


const LINE_CHANGED_MESSAGE =
  "Строку уже изменили в другом окне. Карточка обновлена — проверьте данные и повторите.";

function errorStatus(error: unknown) {
  return (error as { response?: { status?: number } })?.response?.status;
}

function errorText(error: unknown) {
  return procurementErrorText(error);
}

export function ProcurementOrderFormationApp({ bitrixUserName, focusLineId, initialOrder, onBack }: Props) {
  const [order, setOrder] = useState(initialOrder);
  const [lineEdits, setLineEdits] = useState<Record<number, LineEdit>>({});
  const [classificationEdits, setClassificationEdits] = useState<
    Record<number, ClassificationEdit>
  >({});
  const [openedClassification, setOpenedClassification] = useState<number | null>(null);
  const [openedRemoval, setOpenedRemoval] = useState<number | null>(null);
  const [showRemoved, setShowRemoved] = useState(false);
  const [removalReason, setRemovalReason] = useState("");
  const [removalReplacement, setRemovalReplacement] = useState("");
  const [removalWithReplacement, setRemovalWithReplacement] = useState(false);
  const [loadingKey, setLoadingKey] = useState("");
  const [supplierQueries, setSupplierQueries] = useState<Record<number, string>>({});
  const [supplierOptions, setSupplierOptions] = useState<Record<number, ProcurementSupplierOption[]>>({});
  const [distributionPreview, setDistributionPreview] = useState<ProcurementSupplierDistributionPreview | null>(null);
  const [labelSize, setLabelSize] = useState<"50x40" | "40x30">("50x40");
  const [labelPreview, setLabelPreview] = useState<ProcurementOrderLabelPreview | null>(null);
  const [labelOnecNumber, setLabelOnecNumber] = useState(
    initialOrder.label_source?.onec_number || initialOrder.onec_document_number || ""
  );
  const focusedLineRef = useRef<HTMLTableRowElement | null>(null);
  const removalDialogRef = useRef<HTMLDivElement | null>(null);
  const removalReasonRef = useRef<HTMLTextAreaElement | null>(null);
  const removalTriggerRef = useRef<HTMLButtonElement | null>(null);
  const labelSource = order.label_source || (order.onec_document_number
    ? {
        origin: "exchange" as const,
        onec_number: order.onec_document_number,
        onec_date: null,
        linked_at: null,
      }
    : null);
  const labelSourceNumber = labelSource?.onec_number || "";
  const importedFromOnec = order.origin === "onec_import";
  const importedOnecNumber = order.onec_document_number
    || labelSourceNumber
    || order.batch_id.replace(/^onec-/i, "");

  useEffect(() => {
    if (!focusLineId || !focusedLineRef.current) return;
    focusedLineRef.current.scrollIntoView({ behavior: "auto", block: "center" });
    focusedLineRef.current.focus({ preventScroll: true });
  }, [focusLineId]);

  const activeLines = useMemo(() => order.lines.filter((line) => !line.removed), [order.lines]);
  const removedLines = useMemo(() => order.lines.filter((line) => line.removed), [order.lines]);
  const visibleLines = useMemo(
    () => [...order.lines].sort((left, right) => {
      const leftRank = left.removed ? 2 : left.blockers.length > 0 ? 0 : 1;
      const rightRank = right.removed ? 2 : right.blockers.length > 0 ? 0 : 1;
      return leftRank - rightRank || left.line_number - right.line_number;
    }),
    [order.lines]
  );
  const openedRemovalLine = useMemo(
    () => order.lines.find((line) => line.id === openedRemoval) || null,
    [openedRemoval, order.lines]
  );
  // Один и тот же блокер приходит по каждой проблемной строке отдельно, поэтому
  // без группировки экран показывал несколько одинаковых фраз подряд.
  const blockerGroups = useMemo(
    () => groupProcurementBlockers(order.blockers),
    [order.blockers]
  );
  const blockerDetailGroups = useMemo(() => {
    const grouped = new Map<string, { message: string; lines: number[]; severity: string }>();
    (order.blocker_details || []).forEach((detail) => {
      const message = blockerSummaryMessage(detail);
      const key = detail.code;
      const current = grouped.get(key) || { message, lines: [], severity: detail.severity };
      if (detail.line_number != null && !current.lines.includes(detail.line_number)) {
        current.lines.push(detail.line_number);
      }
      grouped.set(key, current);
    });
    return [...grouped.values()].map((item) => ({ ...item, lines: item.lines.sort((a, b) => a - b) }));
  }, [order.blocker_details]);
  const blockingLineNumbers = useMemo(
    () => [...new Set((order.blocker_details || [])
      .map((detail) => detail.line_number)
      .filter((lineNumber): lineNumber is number => typeof lineNumber === "number"))]
      .sort((left, right) => left - right),
    [order.blocker_details]
  );
  const locked = ["approved", "transmitting", "transmitted"].includes(order.status);
  const supplierReviewRoom = !order.supplier_ref && !order.supplier_code;
  const draftTotal = useMemo(
    () => activeLines.reduce((total, line) => {
      const edit = lineEdits[line.id];
      const quantity = Number(edit?.quantity ?? line.final_quantity);
      const price = Number(edit?.price ?? line.purchase_price);
      return total + (Number.isFinite(quantity * price) ? quantity * price : 0);
    }, 0),
    [activeLines, lineEdits]
  );

  const lineEdit = (line: ProcurementOrderFormationLine): LineEdit =>
    lineEdits[line.id] || { quantity: inputNumber(line.final_quantity), price: inputNumber(line.purchase_price) };

  const classificationEdit = (line: ProcurementOrderFormationLine): ClassificationEdit =>
    classificationEdits[line.id] || {
      status: line.effective_assortment_status || "working",
      reason: "",
      manualMinimum: line.manual_minimum ? inputNumber(line.manual_minimum) : "",
      reviewDate: "",
      replacementSkuCode: "",
      noReplacement: false,
    };

  const openReplacementDecision = (line: ProcurementOrderFormationLine) => {
    const current = classificationEdit(line);
    setClassificationEdits((edits) => ({
      ...edits,
      [line.id]: {
        ...current,
        status: "replace_candidate",
        noReplacement: false,
      },
    }));
    setOpenedClassification(line.id);
  };

  const closeRemoval = () => {
    setOpenedRemoval(null);
    setRemovalReason("");
    setRemovalReplacement("");
    setRemovalWithReplacement(false);
    window.setTimeout(() => removalTriggerRef.current?.focus(), 0);
  };

  useEffect(() => {
    if (!openedRemovalLine) return;
    removalReasonRef.current?.focus();
  }, [openedRemovalLine]);

  const handleRemovalDialogKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeRemoval();
      return;
    }
    if (event.key !== "Tab" || !removalDialogRef.current) return;
    const focusable = Array.from(removalDialogRef.current.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href]"
    ));
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  // Версия заказа растёт от любой правки, в том числе в соседней вкладке или у другого
  // закупщика. Поэтому на 409 перезагружаем карточку и повторяем действие, если сама
  // строка не менялась; иначе просим проверить обновлённые данные вручную.
  const runVersioned = async (
    line: ProcurementOrderFormationLine,
    action: (versions: {
      orderVersion: number;
      lineVersion: number;
    }) => Promise<ProcurementOrderFormation>
  ): Promise<ProcurementOrderFormation> => {
    try {
      return await action({ orderVersion: order.version, lineVersion: line.version });
    } catch (error: unknown) {
      if (errorStatus(error) !== 409) throw error;
      const fresh = await fetchProcurementOrder(order.id);
      setOrder(fresh);
      const freshLine = fresh.lines.find((item) => item.id === line.id);
      if (!freshLine || freshLine.version !== line.version) {
        throw new Error(LINE_CHANGED_MESSAGE);
      }
      return action({ orderVersion: fresh.version, lineVersion: freshLine.version });
    }
  };

  const saveLine = async (line: ProcurementOrderFormationLine) => {
    const edit = lineEdit(line);
    setLoadingKey(`line-${line.id}`);
    try {
      const updated = await runVersioned(line, ({ orderVersion, lineVersion }) =>
        updateProcurementOrderLine(order.id, line.id, {
          expected_order_version: orderVersion,
          expected_line_version: lineVersion,
          final_quantity: edit.quantity,
          purchase_price: edit.price,
        })
      );
      setOrder(updated);
      setLineEdits((current) => {
        const next = { ...current };
        delete next[line.id];
        return next;
      });
      toast.success("Строка сохранена, согласование версии снято");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const removeLine = async (line: ProcurementOrderFormationLine) => {
    const reason = removalReason.trim();
    const replacement = removalReplacement.trim();
    if (!reason || (removalWithReplacement && !replacement)) return;
    setLoadingKey(`remove-${line.id}`);
    try {
      const updated = await runVersioned(line, ({ orderVersion, lineVersion }) =>
        updateProcurementOrderLine(order.id, line.id, {
          expected_order_version: orderVersion,
          expected_line_version: lineVersion,
          removed: true,
          removal_reason: reason,
          replacement_sku_code: removalWithReplacement ? replacement : null,
        })
      );
      setOrder(updated);
      closeRemoval();
      toast.success("Строка исключена из проекта; причина сохранена в журнале");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const saveClassification = async (line: ProcurementOrderFormationLine) => {
    const edit = classificationEdit(line);
    setLoadingKey(`class-${line.id}`);
    try {
      const updated = await runVersioned(line, ({ orderVersion, lineVersion }) =>
        createProcurementClassification(order.id, line.id, {
          expected_order_version: orderVersion,
          expected_line_version: lineVersion,
          proposed_status: edit.status,
          reason: edit.reason,
          manual_minimum: edit.manualMinimum || null,
          review_date: edit.reviewDate || null,
          replacement_sku_code: edit.replacementSkuCode.trim() || null,
          no_replacement: edit.noReplacement,
        })
      );
      setOrder(updated);
      setOpenedClassification(null);
      toast.success("Классификация отправлена на отдельное согласование");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const approveClassification = async (line: ProcurementOrderFormationLine) => {
    const proposal = line.latest_classification;
    if (!proposal) return;
    setLoadingKey(`approve-class-${line.id}`);
    try {
      const result = await approveProcurementClassification(order.id, line.id, proposal.id);
      setOrder(result.order);
      toast.success(
        result.mode === "apply"
          ? "Изменение передано в 1С"
          : "Классификация согласована, сформирован dry-run XML"
      );
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const submitOrder = async () => {
    setLoadingKey("submit-order");
    try {
      const result = await submitProcurementOrder(order.id);
      setOrder(result.order);
      toast.success(
        result.mode === "apply"
          ? `Черновик передан в 1С: ${result.message_id}`
          : `Заказ проверен, dry-run без записи в 1С: ${result.message_id}`
      );
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const findSuppliers = async (line: ProcurementOrderFormationLine) => {
    const query = (supplierQueries[line.id] || "").trim();
    if (query.length < 2) {
      toast.error("Введите минимум два символа названия или кода поставщика");
      return;
    }
    setLoadingKey(`supplier-search-${line.id}`);
    try {
      const options = await searchProcurementSupplierOptions(query);
      setSupplierOptions((current) => ({ ...current, [line.id]: options }));
      if (options.length === 0) toast.error("Поставщик не найден в 1С");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const chooseSupplier = async (
    line: ProcurementOrderFormationLine,
    supplier: ProcurementSupplierOption
  ) => {
    setLoadingKey(`supplier-select-${line.id}`);
    try {
      const updated = await selectProcurementLineMainSupplier(order.id, line.id, {
        expected_order_version: order.version,
        expected_line_version: line.version,
        supplier_ref: supplier.ref,
        supplier_code: supplier.code,
        supplier_name: supplier.name,
      });
      setOrder(updated);
      setSupplierOptions((current) => ({ ...current, [line.id]: [] }));
      toast.success("Поставщик выбран; до подтверждения 1С строка остаётся с пометкой");
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const openDistributionPreview = async () => {
    setLoadingKey("supplier-distribution-preview");
    try {
      setDistributionPreview(await previewProcurementSupplierDistribution(order.id));
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const distributeBySuppliers = async () => {
    setLoadingKey("supplier-distribution-apply");
    try {
      const result = await applyProcurementSupplierDistribution(order.id, order.version);
      setOrder(result.source_order);
      setDistributionPreview(null);
      toast.success(`Разнесено строк: ${result.moved_line_count}`);
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const previewLabels = useCallback(async (notifySuccess = true) => {
    setLoadingKey("labels-preview");
    try {
      const preview = await fetchProcurementOrderLabelPreview(order.id, labelSize);
      setLabelPreview(preview);
      if (notifySuccess && preview.ready) {
        toast.success("Весь заказ 1С проверен, этикетки готовы к печати");
      }
    } catch (error: unknown) {
      setLabelPreview(null);
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  }, [labelSize, order.id]);

  const previewMatchesSource = labelPreview?.onec_number === labelSourceNumber
    && labelPreview?.label_size === labelSize;

  useEffect(() => {
    if (!labelSourceNumber || previewMatchesSource) return;
    void previewLabels(false);
  }, [labelSourceNumber, previewMatchesSource, previewLabels]);

  const attachLabelSource = async () => {
    const onecNumber = labelOnecNumber.trim();
    if (!onecNumber) return;
    setLoadingKey("labels-source");
    try {
      const result = await linkProcurementOrderLabelSource(order.id, onecNumber, labelSize);
      setOrder((current) => ({ ...current, label_source: result.label_source }));
      setLabelOnecNumber(result.label_source.onec_number);
      setLabelPreview(result.preview);
      toast.success(`Заказ 1С ${result.label_source.onec_number} подключён и проверен`);
    } catch (error: unknown) {
      setLabelPreview(null);
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const downloadLabels = async (format: "pdf" | "xlsx") => {
    setLoadingKey(`labels-${format}`);
    try {
      const { blob, filename } = await downloadProcurementOrderLabels(
        order.id,
        labelSize,
        format,
        labelPreview?.source_checksum || ""
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      window.setTimeout(() => {
        link.remove();
        URL.revokeObjectURL(url);
      }, 1000);
      toast.success(`${format.toUpperCase()} сформирован`);
    } catch (error: unknown) {
      toast.error(errorText(error));
    } finally {
      setLoadingKey("");
    }
  };

  const canAttachLabelSource = labelSource?.origin !== "exchange";
  const openLinkedProcess = async () => {
    const itemId = order.linked_process?.item_id;
    if (!itemId) return;
    try {
      await openBitrixProcurementProcess(itemId);
    } catch (error: unknown) {
      toast.error(errorText(error));
    }
  };
  const labelSourceForm = (
    <form
      className="order-formation__labels-source"
      onSubmit={(event) => {
        event.preventDefault();
        void attachLabelSource();
      }}
    >
      <label>
        {labelSource ? "Новый номер заказа 1С" : "Номер заказа 1С для всего проекта"}
        <input
          aria-label="Полный номер заказа 1С для этикеток"
          disabled={Boolean(loadingKey)}
          onChange={(event) => setLabelOnecNumber(event.target.value)}
          placeholder="Введите номер заказа 1С"
          value={labelOnecNumber}
        />
        <small>
          Один номер для всех товаров проекта. Например: <strong>РБГУ0000543</strong>.
        </small>
      </label>
      <button
        className={labelSource ? "btn btn--ghost" : "btn"}
        disabled={!labelOnecNumber.trim() || Boolean(loadingKey)}
        type="submit"
      >
        {loadingKey === "labels-source"
          ? "Подключаем весь заказ..."
          : labelSource
            ? "Сохранить другой заказ"
            : "Подключить весь заказ"}
      </button>
    </form>
  );
  const labelsSection = (
    <section className="order-formation__labels" aria-label="Массовые этикетки">
      <div className="order-formation__labels-heading">
        <strong>Этикетки на весь заказ</strong>
        <span>
          {labelSource
            ? `Заказ 1С ${labelSource.onec_number}${labelSource.onec_date ? ` от ${labelSource.onec_date}` : ""}: печатаются все позиции и количества из 1С`
            : "Один раз подключите заказ 1С — товары и количества загрузятся автоматически"}
        </span>
      </div>
      {canAttachLabelSource && (labelSource ? (
        <details className="order-formation__labels-relink">
          <summary>Изменить привязанный заказ 1С</summary>
          {labelSourceForm}
        </details>
      ) : labelSourceForm)}
      <label>
        Размер
        <select
          aria-label="Размер этикетки"
          disabled={Boolean(loadingKey)}
          onChange={(event) => {
            setLabelSize(event.target.value as "50x40" | "40x30");
            setLabelPreview(null);
          }}
          value={labelSize}
        >
          <option value="50x40">50×40 мм</option>
          <option value="40x30">40×30 мм</option>
        </select>
      </label>
      {labelSource ? (
        <button
          className="btn btn--ghost"
          disabled={Boolean(loadingKey)}
          onClick={() => void previewLabels()}
          type="button"
        >
          {loadingKey === "labels-preview" ? "Проверяем весь заказ..." : "Обновить данные из 1С"}
        </button>
      ) : (
        <p className="order-formation__labels-setup-hint">
          После подключения появятся количество страниц и кнопки PDF/XLSX.
        </p>
      )}
      {labelPreview && (
        <div className="order-formation__labels-summary">
          <span>Позиций: <strong>{labelPreview.position_count}</strong></span>
          <span>Этикеток: <strong>{labelPreview.product_label_count}</strong></span>
          <span>Разделителей: <strong>{labelPreview.separator_count}</strong></span>
          <span>
            Страниц: <strong>{labelPreview.total_page_count} / {labelPreview.max_page_count}</strong>
          </span>
        </div>
      )}
      {labelPreview?.blockers.length ? (
        <ul className="order-formation__labels-errors">
          {labelPreview.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
        </ul>
      ) : null}
      {labelPreview?.ready && labelPreview.export_file_count > 1 ? (
        <p className="order-formation__labels-setup-hint">
          Будет создан архив: <strong>{labelPreview.export_file_count} файлов</strong>,
          до {labelPreview.max_page_count} страниц каждый.
        </p>
      ) : null}
      {labelPreview?.rows.length ? (
        <details className="order-formation__labels-rows">
          <summary>Состав этикеток по позициям</summary>
          <div>
            <table>
              <thead><tr><th>Строка</th><th>Товар</th><th>Количество</th></tr></thead>
              <tbody>
                {labelPreview.rows.map((row) => (
                  <tr key={`${row.line_no}-${row.onec_item_code}`}>
                    <td>{row.line_no}</td>
                    <td><strong>{row.onec_item_code}</strong> · {row.item_name}</td>
                    <td>{row.quantity}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
      {labelSource && <div className="order-formation__labels-actions">
        <button
          className="btn"
          disabled={!labelPreview?.ready || Boolean(loadingKey)}
          onClick={() => void downloadLabels("pdf")}
          type="button"
        >
          {loadingKey === "labels-pdf" ? "Формируем..." : "Скачать PDF"}
        </button>
        <button
          className="btn btn--ghost"
          disabled={!labelPreview?.ready || Boolean(loadingKey)}
          onClick={() => void downloadLabels("xlsx")}
          type="button"
        >
          {loadingKey === "labels-xlsx" ? "Формируем..." : "Скачать XLSX"}
        </button>
      </div>}
    </section>
  );

  return (
    <div className="app order-formation">
      <header className="app__header order-formation__header">
        {onBack && <button className="btn btn--ghost" onClick={onBack} type="button">К заказам</button>}
        <div>
          <h1>Формирование заказа</h1>
          <span>
            {ORDER_STATUS_LABELS[order.status] || order.status} · версия {order.version}
          </span>
        </div>
        {bitrixUserName && <span className="app__user">{bitrixUserName}</span>}
      </header>

      <section className="order-formation__conditions">
        <div><span>Поставщик</span><strong>{order.supplier_name}</strong></div>
        <div><span>Договор</span><strong>{order.contract_name}</strong></div>
        <div><span>Склад</span><strong>{order.warehouse_name}</strong></div>
        <div><span>Маршрут</span><strong>{ROUTE_LABELS[order.route] || "Не определён"}</strong></div>
        {importedFromOnec
          ? <div><span>Источник</span><strong>Заказ 1С {importedOnecNumber}</strong></div>
          : <div><span>Партия</span><strong>{order.batch_id}</strong></div>}
        <div><span>Дата</span><strong>{order.order_date}</strong></div>
      </section>

      <section className={`order-formation__linked-process order-formation__linked-process--${order.linked_process?.state || "not_created"}`}>
        <div>
          <span>Связанный процесс</span>
          {order.linked_process?.state === "linked" ? (
            <>
              <strong>Закупка/Заказ №{order.linked_process.item_id}</strong>
              <small>{order.linked_process.stage_name || "Стадия уточняется"}</small>
            </>
          ) : order.linked_process?.state === "broken" ? (
            <>
              <strong>Связь с процессом требует восстановления</strong>
              <small>{order.linked_process.error || "Повторите синхронизацию заказа"}</small>
            </>
          ) : (
            <>
              <strong>Процесс ещё не создан</strong>
              <small>Он появится после создания документа в 1С</small>
            </>
          )}
        </div>
        {order.linked_process?.state === "linked" && order.linked_process.item_id && (
          <button className="btn btn--ghost" onClick={() => void openLinkedProcess()} type="button">
            Открыть процесс
          </button>
        )}
      </section>

      {labelsSection}

      {supplierReviewRoom && !locked && (
        <section className="order-formation__supplier-room">
          <div>
            <strong>Комната разбора поставщиков</strong>
            <span>Назначьте поставщика в строках, затем проверьте будущие проекты.</span>
          </div>
          <button
            className="btn"
            disabled={Boolean(loadingKey)}
            onClick={() => void openDistributionPreview()}
            type="button"
          >
            {loadingKey === "supplier-distribution-preview" ? "Готовим предпросмотр..." : "Разнести по поставщикам"}
          </button>
        </section>
      )}

      {distributionPreview && (
        <section className="order-formation__distribution-preview">
          <header>
            <div>
              <strong>Предпросмотр разнесения</strong>
              <span>В 1С ничего не отправляется.</span>
            </div>
            <button aria-label="Закрыть предпросмотр" onClick={() => setDistributionPreview(null)} type="button">×</button>
          </header>
          {distributionPreview.groups.length > 0 ? (
            <ul>
              {distributionPreview.groups.map((group) => (
                <li key={group.supplier_ref}>
                  <strong>{group.supplier_name}</strong>
                  <span>строки {group.line_numbers.join(", ")} · {group.target_order_id ? `добавятся в проект #${group.target_order_id}` : "будет создан новый проект"}</span>
                </li>
              ))}
            </ul>
          ) : <p>Нет строк с выбранным поставщиком.</p>}
          {distributionPreview.unresolved_line_numbers.length > 0 && (
            <p className="is-warning">Останутся в комнате: строки {distributionPreview.unresolved_line_numbers.join(", ")}.</p>
          )}
          <footer>
            <button className="btn btn--ghost" onClick={() => setDistributionPreview(null)} type="button">Отмена</button>
            <button
              className="btn"
              disabled={distributionPreview.groups.length === 0 || Boolean(loadingKey)}
              onClick={() => void distributeBySuppliers()}
              type="button"
            >
              {loadingKey === "supplier-distribution-apply" ? "Разносим..." : "Подтвердить разнесение"}
            </button>
          </footer>
        </section>
      )}

      {!importedFromOnec && (blockerDetailGroups.length > 0 || blockerGroups.length > 0) && (
        <section className="order-formation__alert">
          <strong>Передача заблокирована</strong>
          <ul>
            {blockerDetailGroups.length > 0
              ? blockerDetailGroups.map((group) => (
                  <li key={`${group.message}-${group.lines.join("-")}`}>
                    {group.message}{group.lines.length ? ` — строки ${group.lines.join(", ")}` : ""}
                  </li>
                ))
              : blockerGroups.map((group) => (
                  <li key={group.text} title={group.codes.join(", ")}>
                    {procurementBlockerText(group)}
                  </li>
                ))}
          </ul>
        </section>
      )}

      <main className="order-formation__body">
        <div className="order-formation__table-wrap">
          <table className="order-formation__table">
            <thead>
              <tr>
                <th>№</th>
                <th>Товар</th>
                <th>Классификация</th>
                <th>Проблема / рекомендация</th>
                <th>Кол-во</th>
                <th>Цена закупки</th>
                <th>Сумма</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visibleLines.map((line, index) => {
                const edit = lineEdit(line);
                const classification = classificationEdit(line);
                const proposal = line.latest_classification;
                const b2bDemand = line.payload?.b2b_customer_demand;
                const problems = lineProblemTexts(line, order.batch_id);
                const recommendationReason = visibleRecommendationReason(line);
                const rounding = roundingExplanation(line);
                const batchDetail = line.blocker_details?.find((detail) => detail.code === "batch_error_suspected");
                const hasBatchBlocker = Boolean(batchDetail || line.blockers.includes("batch_error_suspected"));
                const batchShare = hasBatchBlocker
                  ? batchDetail?.evidence.share_pct ?? payloadValue(line, "batch_error_share_pct")
                  : null;
                const batchReturns = batchDetail?.evidence.return_qty ?? payloadValue(line, "batch_error_return_qty");
                const batchMinimumShare = batchDetail?.evidence.minimum_share_pct;
                const batchMinimumReturns = batchDetail?.evidence.minimum_return_qty;
                const suspectedBatch = batchDetail?.evidence.suspected_batch ?? payloadValue(line, "suspected_batch");
                const supplierDefectConfirmed = line.supplier_defect_attribution === "supplier_exact";
                const supplierDefect = supplierDefectConfirmed ? numeric(line.supplier_defect_pct) : null;
                const productDefect = numeric(line.product_defect_pct ?? payloadValue(line, "defect_share_pct"));
                const firstRemoved = line.removed && (index === 0 || !visibleLines[index - 1].removed);
                return (
                  <Fragment key={line.id}>
                  {firstRemoved && (
                    <tr className="order-formation__removed-summary">
                      <td colSpan={8}>
                        <button
                          aria-expanded={showRemoved}
                          className="btn btn--ghost"
                          onClick={() => setShowRemoved((current) => !current)}
                          type="button"
                        >
                          Исключённые строки: {removedLines.length} · {showRemoved ? "Скрыть" : "Показать"}
                        </button>
                      </td>
                    </tr>
                  )}
                  {(!line.removed || showRemoved) && (
                  <tr
                    className={line.blockers.length || line.removed ? "order-formation__row--blocked" : ""}
                    ref={line.id === focusLineId ? focusedLineRef : undefined}
                    tabIndex={line.id === focusLineId ? -1 : undefined}
                  >
                    <td className="order-formation__line-number">{line.line_number}</td>
                    <td>
                      <strong>{line.nomenclature_name}</strong>
                      <small>1С: {line.nomenclature_code || line.nomenclature_ref}</small>
                      <small>
                        {importedFromOnec && !line.bitrix_product_id
                          ? "Связь с каталогом Bitrix24 обновляется"
                          : `Товар Bitrix24: ${line.bitrix_product_id || "не найден"}`}
                      </small>
                      {line.quality && <small>Качество: {line.quality}</small>}
                      {supplierReviewRoom && !line.removed && (
                        <div className="order-formation__supplier-picker">
                          {line.payload?.main_supplier_selection && (
                            <span className="order-formation__supplier-selection">
                              {line.payload.main_supplier_selection.name} · {line.payload.main_supplier_selection.status === "confirmed_in_1c"
                                ? "подтверждён карточкой 1С"
                                : "выбран, в карточке ещё не записан"}
                            </span>
                          )}
                          <div>
                            <input
                              aria-label={`Поставщик ${line.nomenclature_name}`}
                              disabled={locked}
                              onChange={(event) => setSupplierQueries((current) => ({ ...current, [line.id]: event.target.value }))}
                              placeholder="Название или код поставщика"
                              value={supplierQueries[line.id] || ""}
                            />
                            <button
                              className="btn btn--ghost btn--small"
                              disabled={Boolean(loadingKey)}
                              onClick={() => void findSuppliers(line)}
                              type="button"
                            >
                              Найти
                            </button>
                          </div>
                          {(supplierOptions[line.id] || []).map((supplier) => (
                            <button
                              className="order-formation__supplier-option"
                              disabled={Boolean(loadingKey)}
                              key={supplier.ref}
                              onClick={() => void chooseSupplier(line, supplier)}
                              type="button"
                            >
                              <strong>{supplier.name}</strong><small>{supplier.code}</small>
                            </button>
                          ))}
                        </div>
                      )}
                    </td>
                    <td>
                      <strong>{line.effective_assortment_status_label || "Не задана"}</strong>
                      {line.lifecycle_status &&
                        line.lifecycle_status !== line.effective_assortment_status_label && (
                          <small>Жизненный статус: {line.lifecycle_status}</small>
                        )}
                      {line.procurement_profile && <small>Профиль: {line.procurement_profile}</small>}
                      {proposal && <small>Предложение: {proposal.proposed_status_label} · {proposal.status}</small>}
                      {openedClassification !== line.id && (
                        <button
                          className="btn btn--ghost btn--small"
                          disabled={locked || line.removed}
                          onClick={() => setOpenedClassification(line.id)}
                          type="button"
                        >
                          Изменить классификацию
                        </button>
                      )}
                      {openedClassification !== line.id &&
                        line.display_family_recommendation?.manual_approval_required &&
                        order.manual_status_options.replace_candidate && (
                          <button
                            className="btn btn--ghost btn--small"
                            disabled={locked || line.removed}
                            onClick={() => openReplacementDecision(line)}
                            type="button"
                          >
                            Указать «Взамен ведём»
                          </button>
                        )}
                      {proposal?.status === "proposed" && (
                        <button
                          className="btn btn--small"
                          disabled={Boolean(loadingKey) || locked}
                          onClick={() => approveClassification(line)}
                          type="button"
                        >
                          Согласовать отдельно
                        </button>
                      )}
                      {openedClassification === line.id && (
                        <div className="order-formation__classification">
                          <select
                            aria-label={`Новая классификация ${line.nomenclature_name}`}
                            disabled={locked || line.removed}
                            value={classification.status}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, status: event.target.value },
                            }))}
                          >
                            {Object.entries(order.manual_status_options).map(([value, label]) => (
                              <option key={value} value={value}>{label}</option>
                            ))}
                          </select>
                          <textarea
                            disabled={locked || line.removed}
                            placeholder="Обязательная причина"
                            value={classification.reason}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, reason: event.target.value },
                            }))}
                          />
                          <input
                            aria-label={`Ручной минимум ${line.nomenclature_name}`}
                            disabled={locked || line.removed}
                            min="0"
                            placeholder="Ручной минимум"
                            step="1"
                            type="number"
                            value={classification.manualMinimum}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, manualMinimum: event.target.value },
                            }))}
                          />
                          <input
                            disabled={locked || line.removed}
                            type="date"
                            value={classification.reviewDate}
                            onChange={(event) => setClassificationEdits((current) => ({
                              ...current,
                              [line.id]: { ...classification, reviewDate: event.target.value },
                            }))}
                          />
                          {REPLACEMENT_REQUIRED_STATUSES.has(classification.status) && (
                            <>
                              <input
                                disabled={locked || line.removed || classification.noReplacement}
                                placeholder="Взамен ведём: код 1С (РБ...)"
                                type="text"
                                value={classification.replacementSkuCode}
                                onChange={(event) => setClassificationEdits((current) => ({
                                  ...current,
                                  [line.id]: {
                                    ...classification,
                                    replacementSkuCode: event.target.value,
                                  },
                                }))}
                              />
                              <label className="order-formation__no-replacement">
                                <input
                                  checked={classification.noReplacement}
                                  disabled={locked || line.removed}
                                  type="checkbox"
                                  onChange={(event) => setClassificationEdits((current) => ({
                                    ...current,
                                    [line.id]: {
                                      ...classification,
                                      noReplacement: event.target.checked,
                                      replacementSkuCode: event.target.checked
                                        ? ""
                                        : classification.replacementSkuCode,
                                    },
                                  }))}
                                />
                                Замены нет: снято с производства
                              </label>
                            </>
                          )}
                          <button
                            className="btn btn--small"
                            disabled={
                              !classification.reason.trim() ||
                              (REPLACEMENT_REQUIRED_STATUSES.has(classification.status) &&
                                !classification.replacementSkuCode.trim() &&
                                !classification.noReplacement) ||
                              Boolean(loadingKey) ||
                              locked
                            }
                            onClick={() => saveClassification(line)}
                            type="button"
                          >
                            {classification.status === "pension" ? "Перевести в Допродаём" : "На согласование"}
                          </button>
                          <button
                            className="btn btn--ghost btn--small"
                            onClick={() => setOpenedClassification(null)}
                            type="button"
                          >
                            Закрыть
                          </button>
                        </div>
                      )}
                    </td>
                    <td>
                      <strong>{quantity(line.recommended_quantity)} шт.</strong>
                      {problems.length > 0 && (
                        <strong className="is-warning">Проблема: {problems[0]}</strong>
                      )}
                      {problems.slice(1).map((problem) => (
                        <small className="is-warning" key={problem}>Также: {problem}</small>
                      ))}
                      {line.payload?.recommendation_discrepancy?.final_quantity && (
                        <small className="is-warning">
                          Решение человека: {quantity(line.payload.recommendation_discrepancy.final_quantity.manual)} · новый расчёт: {quantity(line.payload.recommendation_discrepancy.final_quantity.recommended)}
                        </small>
                      )}
                      {line.payload?.recommendation_discrepancy?.purchase_price && (
                        <small className="is-warning">
                          Цена человека: {money(line.payload.recommendation_discrepancy.purchase_price.manual, line.currency)} · новая цена: {money(line.payload.recommendation_discrepancy.purchase_price.recommended, line.currency)}
                        </small>
                      )}
                      {recommendationReason && <small>Рекомендация: {recommendationReason}</small>}
                      {numeric(batchShare) !== null ? (
                        <div className="order-formation__evidence">
                          <strong>Возвраты партии: {percent(batchShare)}</strong>
                          <small>
                            {returnLabel(batchReturns)} · порог {batchMinimumReturns == null ? "—" : returnLabel(batchMinimumReturns)} и {percent(batchMinimumShare)}
                          </small>
                          <small>Партия: {batchName(suspectedBatch)}</small>
                          <small>Подтверждённый брак поставщика: {supplierDefect === null ? "данных нет" : percent(supplierDefect)}</small>
                        </div>
                      ) : supplierDefect !== null ? (
                        <small>Подтверждённый брак поставщика: {percent(supplierDefect)}</small>
                      ) : productDefect !== null ? (
                        <small>Брак товара: {percent(productDefect)} · поставщик не подтверждён</small>
                      ) : (
                        <small>Данных о браке нет</small>
                      )}
                      <small title="Доля прибыли в обороте, 180 дней">
                        Рентабельность: {profitabilityText(line)}
                      </small>
                      {rounding && <small>{rounding}</small>}
                      {b2bDemand && (
                        <div className="order-formation__b2b-advisory">
                          <strong>
                            Клиенты 3/4/5: {b2bDemand.replacement_recommended_order_qty || "0"} шт.
                          </strong>
                          <small>
                            Альтернативный расчёт, основной заказ не изменён
                            {b2bDemand.order_delta_qty
                              ? ` · разница ${Number(b2bDemand.order_delta_qty) > 0 ? "+" : ""}${b2bDemand.order_delta_qty} шт.`
                              : ""}
                          </small>
                          <small>
                            Активных: {b2bDemand.active_customer_count ?? 0} · пассивных:{" "}
                            {b2bDemand.passive_customer_count ?? 0} · ожидаются к сроку:{" "}
                            {b2bDemand.due_customer_count ?? 0}
                          </small>
                          {b2bDemand.dependency_class && <small>{b2bDemand.dependency_class}</small>}
                          {b2bDemand.reason_ru && (
                            <details>
                              <summary>Почему так рассчитано</summary>
                              <small>{b2bDemand.reason_ru}</small>
                            </details>
                          )}
                        </div>
                      )}
                    </td>
                    <td>
                      <input
                        aria-label={`Количество ${line.nomenclature_name}`}
                        min="0"
                        disabled={locked || line.removed}
                        step="1"
                        type="number"
                        value={edit.quantity}
                        onChange={(event) => setLineEdits((current) => ({
                          ...current,
                          [line.id]: { ...edit, quantity: event.target.value },
                        }))}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Цена закупки ${line.nomenclature_name}`}
                        min="0"
                        disabled={locked || line.removed}
                        step="0.01"
                        type="number"
                        value={edit.price}
                        onChange={(event) => setLineEdits((current) => ({
                          ...current,
                          [line.id]: { ...edit, price: event.target.value },
                        }))}
                      />
                    </td>
                    <td><strong>{money(String(Number(edit.quantity) * Number(edit.price)), line.currency)}</strong></td>
                    <td>
                      <button
                        className="btn btn--ghost btn--small"
                        disabled={Boolean(loadingKey) || locked || line.removed}
                        onClick={() => saveLine(line)}
                        type="button"
                      >
                        Сохранить количество и цену
                      </button>
                      {line.product_card_url && (
                        <a
                          className="order-formation__product-card-link"
                          href={line.product_card_url}
                          rel="noreferrer"
                          target="_blank"
                        >
                          Открыть карточку
                        </a>
                      )}
                      {line.blockers.length > 0 && !line.removed && !locked && openedRemoval !== line.id && (
                        <button
                          className="btn btn--ghost btn--small"
                          ref={(node) => {
                            if (node && openedRemoval === null) removalTriggerRef.current = node;
                          }}
                          onClick={() => {
                            removalTriggerRef.current = document.activeElement as HTMLButtonElement;
                            setOpenedRemoval(line.id);
                            setRemovalReason("");
                            setRemovalReplacement("");
                            setRemovalWithReplacement(false);
                          }}
                          type="button"
                        >
                          Исключить строку
                        </button>
                      )}
                    </td>
                  </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </main>

      {openedRemovalLine && (
        <div
          className="order-formation__dialog-overlay"
          onKeyDown={handleRemovalDialogKeyDown}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeRemoval();
          }}
        >
          <div
            aria-describedby="order-removal-description"
            aria-labelledby="order-removal-title"
            aria-modal="true"
            className="order-formation__removal-dialog"
            ref={removalDialogRef}
            role="dialog"
          >
            <header>
              <div>
                <h2 id="order-removal-title">Исключить строку {openedRemovalLine.line_number}</h2>
                <p id="order-removal-description">{openedRemovalLine.nomenclature_name}</p>
              </div>
              <button aria-label="Закрыть форму исключения" onClick={closeRemoval} type="button">×</button>
            </header>
            <form onSubmit={(event) => { event.preventDefault(); void removeLine(openedRemovalLine); }}>
              <label>
                Причина исключения <span aria-hidden="true">*</span>
                <textarea
                  ref={removalReasonRef}
                  onChange={(event) => setRemovalReason(event.target.value)}
                  placeholder="Обязательно укажите, почему строку исключают"
                  required
                  value={removalReason}
                />
              </label>
              <label className="order-formation__no-replacement">
                <input
                  checked={removalWithReplacement}
                  onChange={(event) => setRemovalWithReplacement(event.target.checked)}
                  type="checkbox"
                />
                Указать «Взамен ведём»
              </label>
              {removalWithReplacement && (
                <label>
                  Взамен ведём
                  <input
                    aria-label={`Взамен ведём для ${openedRemovalLine.nomenclature_name}`}
                    onChange={(event) => setRemovalReplacement(event.target.value)}
                    placeholder="Код 1С карточки (РБ...)"
                    required
                    value={removalReplacement}
                  />
                </label>
              )}
              <footer>
                <button className="btn btn--ghost" onClick={closeRemoval} type="button">Отмена</button>
                <button
                  className="btn"
                  disabled={
                    !removalReason.trim() ||
                    (removalWithReplacement && !removalReplacement.trim()) ||
                    Boolean(loadingKey)
                  }
                  type="submit"
                >
                  {loadingKey === `remove-${openedRemovalLine.id}` ? "Исключаем..." : "Исключить из проекта"}
                </button>
              </footer>
            </form>
          </div>
        </div>
      )}

      <footer className="order-formation__footer">
        <span>{countLabel(activeLines.length, "строка", "строки", "строк")}</span>
        <strong>Итого: {money(String(draftTotal), order.currency)}</strong>
        <span>1С: {ONEC_STATUS_LABELS[order.onec_status] || "Статус не определён"}</span>
        {order.approved_by_name && <span>Согласовал: {order.approved_by_name}</span>}
        {!locked && !importedFromOnec && (
          <div className="order-formation__submit-action">
            <button
              aria-describedby={order.blockers.length > 0 ? "order-submit-blocked-hint" : undefined}
              className="btn"
              disabled={Boolean(loadingKey) || order.blockers.length > 0}
              onClick={submitOrder}
              type="button"
            >
              {loadingKey === "submit-order" ? "Проверяем и передаём..." : "Проверить и создать черновик в 1С"}
            </button>
            {order.blockers.length > 0 && (
              <small id="order-submit-blocked-hint">
                Сначала разберите {blockingLineNumbers.length > 0
                  ? `строки ${blockingLineNumbers.join(", ")}`
                  : "блокирующие условия выше"}.
              </small>
            )}
          </div>
        )}
      </footer>
    </div>
  );
}
