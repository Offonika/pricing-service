import { useEffect, useState } from "react";
import {
  fetchProcurementOrderFormation,
  type ProcurementOrderFormation,
} from "../api/procurementAssortment";
import { ProcurementAssortmentDecisionApp } from "./ProcurementAssortmentDecisionApp";
import { ProcurementOrderFormationApp } from "./ProcurementOrderFormationApp";

interface Props {
  bitrixUserName?: string | null;
  itemId: string;
}

export function ProcurementAssortmentWorkspace({ bitrixUserName, itemId }: Props) {
  const [order, setOrder] = useState<ProcurementOrderFormation | null>(null);
  const [legacy, setLegacy] = useState(false);
  const [message, setMessage] = useState("Загрузка пакетного заказа...");

  useEffect(() => {
    let cancelled = false;
    fetchProcurementOrderFormation(itemId)
      .then((data) => {
        if (!cancelled) setOrder(data);
      })
      .catch((error: unknown) => {
        const status = (error as { response?: { status?: number } })?.response?.status;
        if (!cancelled && status === 404) {
          setLegacy(true);
          return;
        }
        if (!cancelled) {
          setMessage(error instanceof Error ? error.message : "Не удалось загрузить заказ");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [itemId]);

  if (legacy) {
    return <ProcurementAssortmentDecisionApp bitrixUserName={bitrixUserName} itemId={itemId} />;
  }
  if (order) {
    return <ProcurementOrderFormationApp bitrixUserName={bitrixUserName} initialOrder={order} />;
  }
  return (
    <div className="app app--center">
      <div className="app-state"><h1>Формирование заказа</h1><p>{message}</p></div>
    </div>
  );
}
