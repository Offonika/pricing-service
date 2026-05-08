import { api, clearApiAuthToken, setApiAuthToken } from "./client";

const BITRIX_SDK_URL = "https://api.bitrix24.com/api/v1/";
const SESSION_STORAGE_KEY = "mm_matching_bitrix_session";
const LEFT_MENU_STORAGE_KEY = "mm_matching_bitrix_left_menu_bound";
const REFRESH_SKEW_MS = 60_000;
const MATCHING_LEFT_MENU_PLACEMENT = "LEFT_MENU";
const MATCHING_LEFT_MENU_TITLE = "Сопоставление товаров";

interface BitrixAuthPayload {
  access_token: string;
  domain: string;
  member_id: string;
}

interface BitrixLaunchPayload {
  access_token?: string | null;
  domain?: string | null;
  member_id?: string | null;
}

interface BitrixMatchingUser {
  user_id: string;
  name?: string | null;
}

interface BitrixMatchingSessionResponse {
  session_token: string;
  expires_at: string;
  expires_in: number;
  user: BitrixMatchingUser;
}

interface CachedBitrixSession extends BitrixMatchingSessionResponse {
  cached_at: string;
}

interface BX24CallResult<T> {
  data(): T;
  error(): string | false;
  error_description(): string;
}

interface BX24Api {
  init(callback: () => void): void;
  getAuth(): false | BitrixAuthPayload;
  callMethod<T>(
    method: string,
    params: Record<string, unknown>,
    callback: (result: BX24CallResult<T>) => void
  ): void;
}

declare global {
  interface Window {
    BX24?: BX24Api;
    __MM_BITRIX_LAUNCH__?: BitrixLaunchPayload;
  }
}

export function isBitrixMatchingRoute() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path === "/bitrix/matching" || path.startsWith("/bitrix/matching/");
}

function readCachedSession(): CachedBitrixSession | null {
  try {
    const raw = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedBitrixSession;
    if (Date.parse(cached.expires_at) - Date.now() <= REFRESH_SKEW_MS) {
      window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
      return null;
    }
    return cached;
  } catch {
    return null;
  }
}

function cacheSession(session: BitrixMatchingSessionResponse) {
  const cached: CachedBitrixSession = {
    ...session,
    cached_at: new Date().toISOString(),
  };
  try {
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(cached));
  } catch {
    // Storage can be restricted in embedded contexts; in-memory axios auth still works.
  }
}

function loadBitrixSdk() {
  if (window.BX24) return Promise.resolve();

  return new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${BITRIX_SDK_URL}"]`);
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error("Не удалось загрузить Bitrix24 SDK")), {
        once: true,
      });
      return;
    }

    const script = document.createElement("script");
    script.src = BITRIX_SDK_URL;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Не удалось загрузить Bitrix24 SDK"));
    document.head.appendChild(script);
  });
}

function getLaunchAuth(): BitrixAuthPayload | null {
  const launch = window.__MM_BITRIX_LAUNCH__;
  if (!launch?.access_token || !launch.domain || !launch.member_id) {
    return null;
  }
  return {
    access_token: launch.access_token,
    domain: launch.domain,
    member_id: launch.member_id,
  };
}

function initBitrix() {
  return new Promise<BitrixAuthPayload>((resolve, reject) => {
    if (!window.BX24) {
      reject(new Error("Bitrix24 SDK недоступен"));
      return;
    }

    window.BX24.init(() => {
      const auth = window.BX24?.getAuth();
      if (!auth) {
        reject(new Error("Bitrix24 не вернул OAuth-сессию"));
        return;
      }
      resolve(auth);
    });
  });
}

function bitrixCall<T>(method: string, params: Record<string, unknown>) {
  return new Promise<T>((resolve, reject) => {
    if (!window.BX24) {
      reject(new Error("Bitrix24 SDK недоступен"));
      return;
    }
    window.BX24.callMethod<T>(method, params, (result) => {
      const error = result.error();
      if (error) {
        reject(new Error(`${error}: ${result.error_description()}`));
        return;
      }
      resolve(result.data());
    });
  });
}

function normalizeHandlerUrl(value: string) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return value.trim().replace(/\/+$/, "");
  }
}

function matchingHandlerUrl() {
  return new URL("/bitrix/matching/", window.location.origin).toString();
}

async function ensureBitrixLeftMenuPlacement() {
  try {
    if (window.sessionStorage.getItem(LEFT_MENU_STORAGE_KEY) === "1") return;
  } catch {
    // Ignore restricted storage in embedded contexts.
  }

  await loadBitrixSdk();
  await initBitrix();

  const handler = matchingHandlerUrl();
  const normalizedHandler = normalizeHandlerUrl(handler);
  const placements = await bitrixCall<
    Array<{ placement?: string; handler?: string; title?: string }>
  >("placement.get", {});
  const alreadyBound = placements.some(
    (item) =>
      item.placement === MATCHING_LEFT_MENU_PLACEMENT &&
      normalizeHandlerUrl(String(item.handler || "")) === normalizedHandler
  );
  if (!alreadyBound) {
    await bitrixCall<boolean>("placement.bind", {
      PLACEMENT: MATCHING_LEFT_MENU_PLACEMENT,
      HANDLER: handler,
      TITLE: MATCHING_LEFT_MENU_TITLE,
      DESCRIPTION: "Bitrix Matching",
      LANG_ALL: {
        ru: {
          TITLE: MATCHING_LEFT_MENU_TITLE,
          DESCRIPTION: "Сопоставление товаров",
        },
        en: {
          TITLE: "Product matching",
          DESCRIPTION: "Product matching",
        },
      },
    });
  }

  try {
    window.sessionStorage.setItem(LEFT_MENU_STORAGE_KEY, "1");
  } catch {
    // Storage can be restricted in embedded contexts; binding already succeeded.
  }
}

function ensureBitrixLeftMenuPlacementInBackground() {
  ensureBitrixLeftMenuPlacement().catch((error: unknown) => {
    console.warn("Не удалось добавить приложение в левое меню Bitrix24", error);
  });
}

export async function initializeBitrixMatchingSession() {
  const cached = readCachedSession();
  if (cached) {
    setApiAuthToken(cached.session_token);
    ensureBitrixLeftMenuPlacementInBackground();
    return cached.user;
  }

  clearApiAuthToken();
  let auth = getLaunchAuth();
  if (!auth) {
    await loadBitrixSdk();
    auth = await initBitrix();
  }
  const { data } = await api.post<BitrixMatchingSessionResponse>("/bitrix/matching/session", {
    access_token: auth.access_token,
    domain: auth.domain,
    member_id: auth.member_id,
  });
  setApiAuthToken(data.session_token);
  cacheSession(data);
  ensureBitrixLeftMenuPlacementInBackground();
  return data.user;
}
