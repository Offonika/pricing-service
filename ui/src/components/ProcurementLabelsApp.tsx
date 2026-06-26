import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  approveProcurementLabels,
  fetchProcurementLabelPreview,
  generateProcurementLabels,
  type ProcurementLabelPreview,
  type ProcurementLabelRow,
} from "../api/procurementLabels";

interface ProcurementLabelsAppProps {
  bitrixUserName?: string | null;
  itemId: string;
}

function formatQuantity(value: string) {
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return value;
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 }).format(numberValue);
}

function statusLabel(value: string) {
  if (value === "ready") return "Готово";
  if (value === "blocked") return "Стоп";
  if (value === "covered") return "ДС есть";
  if (value === "missing") return "Нет ДС";
  return value || "Пусто";
}

function RowStatus({ row }: { row: ProcurementLabelRow }) {
  const blocked = row.status === "blocked";
  return (
    <span className={blocked ? "proc-labels__status proc-labels__status--blocked" : "proc-labels__status"}>
      {statusLabel(row.status)}
    </span>
  );
}

export function ProcurementLabelsApp({ bitrixUserName, itemId }: ProcurementLabelsAppProps) {
  const [preview, setPreview] = useState<ProcurementLabelPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState<"generate" | "approve" | null>(null);
  const [message, setMessage] = useState("");

  const blockers = useMemo(() => preview?.blockers || [], [preview]);
  const canGenerate = Boolean(preview?.ready && preview.rows.length && !actionLoading);
  const canApprove = Boolean((preview?.zip_url || preview?.artifact_version) && !preview?.blocked && !actionLoading);

  const refresh = useCallback(async () => {
    if (!itemId) return;
    setLoading(true);
    setMessage("");
    try {
      const data = await fetchProcurementLabelPreview(itemId);
      setPreview(data);
      setMessage(data.blocked ? "Есть строки, которые нужно исправить до печати." : "Проверка пройдена.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Не удалось проверить этикетки");
    } finally {
      setLoading(false);
    }
  }, [itemId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const generate = async () => {
    if (!itemId || !canGenerate) return;
    setActionLoading("generate");
    try {
      const data = await generateProcurementLabels(itemId);
      setPreview(data.preview);
      if (data.generated) {
        toast.success("ZIP сформирован");
        setMessage(`Сформирована версия v${data.artifact_version || data.preview.artifact_version}.`);
      } else {
        setMessage("ZIP не сформирован: есть стоп-ошибки.");
      }
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Не удалось сформировать ZIP");
    } finally {
      setActionLoading(null);
    }
  };

  const approve = async () => {
    if (!itemId || !canApprove) return;
    setActionLoading("approve");
    try {
      const data = await approveProcurementLabels(itemId);
      toast.success("Версия утверждена");
      setMessage(`Статус обновлен: ${data.status}.`);
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Не удалось утвердить версию");
    } finally {
      setActionLoading(null);
    }
  };

  if (!itemId) {
    return (
      <div className="app app--center">
        <div className="app-state">
          <h1>Этикетки ВЭД</h1>
          <p>Bitrix24 не передал ID карточки закупки.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="app proc-labels">
      <header className="app__header proc-labels__header">
        <h1>Этикетки ВЭД</h1>
        {bitrixUserName && <span className="app__user">{bitrixUserName}</span>}
        <button className="btn btn--ghost" disabled={loading} onClick={refresh} type="button">
          Проверить
        </button>
        <button className="btn" disabled={!canGenerate} onClick={generate} type="button">
          {actionLoading === "generate" ? "Формируем..." : "Сформировать ZIP"}
        </button>
        <button className="btn btn--ghost" disabled={!canApprove} onClick={approve} type="button">
          {actionLoading === "approve" ? "Утверждаем..." : "Утвердить"}
        </button>
      </header>

      <section className="proc-labels__summary">
        <div>
          <span>Карточка</span>
          <strong>{preview?.item_id || itemId}</strong>
        </div>
        <div>
          <span>1С заказ</span>
          <strong>{preview?.onec_number || "..."}</strong>
        </div>
        <div>
          <span>Строк</span>
          <strong>{preview?.rows.length ?? 0}</strong>
        </div>
        <div>
          <span>Статус</span>
          <strong>{preview?.blocked ? "Есть стоп-ошибки" : preview ? "Готово к ZIP" : "Проверяем"}</strong>
        </div>
        {preview?.zip_url && (
          <a className="btn btn--ghost proc-labels__download" href={preview.zip_url} rel="noreferrer" target="_blank">
            Скачать ZIP
          </a>
        )}
      </section>

      {message && <div className="proc-labels__message">{message}</div>}

      {blockers.length > 0 && (
        <section className="proc-labels__errors">
          <strong>Что исправить перед печатью</strong>
          <ul>
            {blockers.slice(0, 12).map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          {blockers.length > 12 && <small>Еще ошибок: {blockers.length - 12}</small>}
        </section>
      )}

      <section className="proc-labels__table-wrap">
        {loading && <div className="proc-labels__state">Проверяем строки заказа...</div>}
        {!loading && !preview?.rows.length && <div className="proc-labels__state">Строки заказа пока не найдены.</div>}
        {Boolean(preview?.rows.length) && (
          <table className="proc-labels__table">
            <thead>
              <tr>
                <th>Стр.</th>
                <th>Товар</th>
                <th>1С</th>
                <th>SKU</th>
                <th>Barcode/GTIN</th>
                <th>Кол-во</th>
                <th>ДС/EAC</th>
                <th>Статус</th>
              </tr>
            </thead>
            <tbody>
              {preview?.rows.map((row) => (
                <tr className={row.status === "blocked" ? "proc-labels__row--blocked" : ""} key={row.line_no}>
                  <td>{row.line_no}</td>
                  <td>
                    <strong>{row.item_name}</strong>
                    {row.blockers.length > 0 && <em>{row.blockers.join("; ")}</em>}
                  </td>
                  <td>{row.onec_item_code || "..."}</td>
                  <td>{row.sku || "..."}</td>
                  <td>{row.barcode || "..."}</td>
                  <td>
                    {formatQuantity(row.quantity)} {row.unit}
                  </td>
                  <td>
                    {row.certificate_id || statusLabel(row.certificate_status)}
                    {row.eac_allowed && <span className="proc-labels__eac">EAC</span>}
                  </td>
                  <td>
                    <RowStatus row={row} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
