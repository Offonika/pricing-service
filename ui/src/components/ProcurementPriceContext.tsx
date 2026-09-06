import { useEffect, useId, useRef, useState } from "react";
import type { ProcurementPriceContext as PriceContext, ProcurementPriceFact } from "../api/procurementAssortment";
import "./ProcurementPriceContext.css";

const number = (value: string) => Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 4 });
const dateText = (value: string) => new Date(value).toLocaleDateString("ru-RU");

function Fact({ fact }: { fact: ProcurementPriceFact }) {
  const missing = fact.status === "unconfirmed" ? "Цена не согласована"
    : fact.reason === "see_individual_receipt_prices" ? "См. стоимость по отдельным поступлениям"
    : fact.status === "ambiguous" ? "Требует уточнения единицы, характеристики или источника"
      : fact.reason === "applicable_document_rate_missing" ? "Нет подтверждённого курса для этой закупки"
        : "Нет подтверждённых данных";
  return <div className="procurement-price-context__fact">
    <strong>{fact.value === null ? missing : `${number(fact.value)} ${fact.currency || "(валюта не определена)"}`}</strong>
    {fact.value !== null && <small>За {fact.unit_name || "единицу (не уточнена)"}{fact.at ? ` · ${dateText(fact.at)}` : ""}</small>}
    {fact.confirmed_by && <small>Подтвердил: {fact.confirmed_by}</small>}
    {fact.exchange_rate && fact.exchange_multiplicity && <small>
      Курс {number(fact.exchange_rate)} · кратность {number(fact.exchange_multiplicity)}
      {fact.exchange_rate_at ? ` · ${dateText(fact.exchange_rate_at)}` : ""}
    </small>}
    {fact.characteristic_ref && !/^0x0+$/.test(fact.characteristic_ref) && <small title={fact.characteristic_ref}>Для указанной характеристики 1С</small>}
    {fact.documents.map((document, index) => <small key={`${document.ref}-${index}`} title={`${document.kind}: ${document.ref}`}>
      {document.kind}: {document.number || "номер не получен"}{document.at ? ` от ${dateText(document.at)}` : ""}
    </small>)}
  </div>;
}

export function ProcurementPriceContext({ context, productName }: { context?: PriceContext | null; productName: string }) {
  const [opened, setOpened] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  useEffect(() => {
    if (opened) dialog.current?.showModal();
  }, [opened]);
  if (!context) return null;
  return <div className="procurement-price-context">
    <button className="btn btn--ghost btn--small" type="button" aria-haspopup="dialog" aria-label={`Цена, курс и себестоимость: ${productName}`} onClick={() => setOpened(true)}>Цена, курс и себестоимость</button>
    {opened && <dialog className="procurement-price-context__dialog" ref={dialog} aria-labelledby={headingId} onClose={() => setOpened(false)}>
    <header><div><h2 id={headingId}>Цена, курс и себестоимость</h2><p>{productName}</p></div>
      <button className="btn btn--ghost btn--small" type="button" onClick={() => dialog.current?.close()} aria-label="Закрыть ценовой контекст">Закрыть</button>
    </header>
    <div className="procurement-price-context__body">
      {context.stale && <p role="status">Источник недоступен. Показаны последние подтверждённые данные{context.last_success_on ? ` от ${dateText(context.last_success_on)}` : ""}; требуется обновление.</p>}
      {context.source_status === "not_loaded" && <p>Учётные цены ещё не загружены.</p>}
      <dl>
        <div><dt>Закупочная цена в валюте поставщика</dt><dd><Fact fact={context.agreed_purchase} /></dd></div>
        <div><dt>Закупочная стоимость в рублях</dt><dd><Fact fact={context.purchase_rub} /></dd></div>
        <div><dt>Себестоимость в рублях · справочно</dt><dd><Fact fact={context.reference_cost_rub} /><small>Последняя подтверждённая по товару. В сумму нового заказа не входит.</small></dd></div>
      </dl>
      {context.receipt_purchases_rub.length > 0 && context.purchase_rub.status === "ambiguous" && <>
        <strong>Закупочная стоимость по поступлениям · в рублях</strong>
        {context.receipt_purchases_rub.map((fact, index) => <Fact key={index} fact={fact} />)}
      </>}
      <p>Себестоимость включает распределённые допрасходы. Её отличие от закупочной стоимости ожидаемо.</p>
      <strong>Фактическая себестоимость новой поставки</strong>
      {context.actual_costs_rub.length === 0 ? <p>Пока не подтверждена связанными документами поступления и окончательного распределения допрасходов.</p>
        : context.actual_costs_rub.map((fact, index) => <Fact key={index} fact={fact} />)}
      {context.actual_cost_status === "partial" && <p>Подтверждена для части поступлений. По остальным ожидаются данные.</p>}
      <strong>Записи цен поставщика · справочно</strong>
      {context.supplier_quotes.length ? <><p>Запись в 1С требует проверки актуальности у поставщика. Цена заказа сохраняется отдельно.</p>
        {context.supplier_quotes.map((fact, index) => <Fact key={index} fact={fact} />)}</>
        : <p>{context.source_status === "ready" ? "Записи для этого поставщика и товара не найдены." : "Данные о ценах поставщика не подтверждены."}</p>}
      {context.checked_on && <small>Проверка источника: {dateText(context.checked_on)}</small>}
    </div>
    </dialog>}
  </div>;
}
