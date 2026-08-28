import { useEffect, useState } from "react";
import { initializeBitrixLogisticsSession } from "./api/bitrix";
import { LogisticsWorkspace } from "./components/LogisticsWorkspace";

type AuthState =
  | { status: "loading" }
  | { status: "ready" }
  | { status: "error"; message: string };

function returnToBitrix() {
  const launch = window.__MM_BITRIX_LAUNCH__;
  if (launch?.domain) {
    window.top?.location.assign(`https://${launch.domain}/`);
    return;
  }
  window.history.back();
}

export function BitrixLogisticsApp() {
  const [authState, setAuthState] = useState<AuthState>({ status: "loading" });
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const [slowConnection, setSlowConnection] = useState(false);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = "Логистика — Bitrix24";
    return () => {
      document.title = previousTitle;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const slowTimer = window.setTimeout(() => {
      if (!cancelled) setSlowConnection(true);
    }, 8000);
    initializeBitrixLogisticsSession()
      .then(() => {
        if (!cancelled) setAuthState({ status: "ready" });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setAuthState({
            status: "error",
            message: error instanceof Error ? error.message : "Нет доступа к логистике",
          });
        }
      })
      .finally(() => window.clearTimeout(slowTimer));
    return () => {
      cancelled = true;
      window.clearTimeout(slowTimer);
    };
  }, [connectionAttempt]);

  if (authState.status !== "ready") {
    const openedOutsideBitrix =
      authState.status === "error" &&
      (authState.message.includes("Bitrix24 SDK") || authState.message.includes("OAuth"));
    const accessDenied =
      authState.status === "error" &&
      /(?:status code\s+(?:401|403)|\b(?:unauthorized|forbidden)\b)/i.test(authState.message);
    return (
      <div className="app app--center">
        <div className="app-state app-state--wide">
          <h1>Логистика</h1>
          {authState.status === "loading" && (
            <p>
              {slowConnection
                ? "Bitrix24 отвечает дольше обычного. Можно повторить подключение."
                : "Подключение к Bitrix24…"}
            </p>
          )}
          {authState.status === "error" && (
            <>
              <p>
                {openedOutsideBitrix
                  ? "Откройте приложение из меню Bitrix24."
                  : accessDenied
                    ? "Нет доступа к логистике. Проверьте роль и привязку склада."
                    : "Не удалось подключиться к логистике. Повторите запуск."}
              </p>
              <small>{authState.message}</small>
            </>
          )}
          {(authState.status === "error" || slowConnection) && (
            <div className="app-state__actions">
              <button
                className="btn"
                type="button"
                onClick={() => {
                  setAuthState({ status: "loading" });
                  setSlowConnection(false);
                  setConnectionAttempt((attempt) => attempt + 1);
                }}
              >
                Повторить
              </button>
              <button className="btn btn--ghost" type="button" onClick={returnToBitrix}>
                Вернуться в Bitrix24
              </button>
            </div>
          )}
        </div>
      </div>
    );
  }
  return <LogisticsWorkspace />;
}
