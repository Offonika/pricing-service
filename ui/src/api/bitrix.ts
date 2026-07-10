import { api, clearApiAuthToken, setApiAuthToken } from "./client";

const BITRIX_SDK_URL = "https://api.bitrix24.com/api/v1/";
const SESSION_STORAGE_KEY = "mm_matching_bitrix_session";
const LEFT_MENU_STORAGE_KEY = "mm_matching_bitrix_left_menu_bound";
const RECEIVABLES_SESSION_STORAGE_KEY = "mm_receivables_bitrix_session";
const RECEIVABLES_LEFT_MENU_STORAGE_KEY = "mm_receivables_bitrix_left_menu_bound";
const EXECUTIVE_SESSION_STORAGE_KEY = "mm_executive_dashboard_bitrix_session";
const EXECUTIVE_LEFT_MENU_STORAGE_KEY = "mm_executive_dashboard_bitrix_left_menu_bound";
const PROCUREMENT_LABELS_SESSION_STORAGE_KEY = "mm_procurement_labels_bitrix_session";
const PROCUREMENT_LABELS_PLACEMENT_STORAGE_KEY = "mm_procurement_labels_bitrix_tab_bound";
const PROCUREMENT_ASSORTMENT_SESSION_STORAGE_KEY = "mm_procurement_assortment_bitrix_session";
const PROCUREMENT_ORDER_FORMATION_SESSION_STORAGE_KEY =
  "mm_procurement_order_formation_bitrix_session";
const PROCUREMENT_ORDER_FORMATION_PLACEMENT_STORAGE_KEY =
  "mm_procurement_order_formation_left_menu_v3_bound";
const REFRESH_SKEW_MS = 60_000;
const MATCHING_LEFT_MENU_PLACEMENT = "LEFT_MENU";
const PROCUREMENT_LABELS_DETAIL_PLACEMENT = "CRM_DYNAMIC_1056_DETAIL_TAB";
const MATCHING_LEFT_MENU_TITLE = "Сопоставление товаров";
const RECEIVABLES_LEFT_MENU_TITLE = "Дебиторка покупателей";
const EXECUTIVE_LEFT_MENU_TITLE = "Управленческая витрина";
const PROCUREMENT_LABELS_TITLE = "Сформировать этикетки";
const PROCUREMENT_ORDER_FORMATION_MENU_TITLE = "Формирование заказа";

interface BitrixAuthPayload {
  access_token: string;
  domain: string;
  member_id: string;
}

interface BitrixLaunchPayload {
  access_token?: string | null;
  domain?: string | null;
  member_id?: string | null;
  placement?: string | null;
  placement_options?: Record<string, unknown> | null;
}

interface BitrixMatchingUser {
  user_id: string;
  name?: string | null;
}

export type BitrixReceivablesAccessLevel = "full" | "department";
export type BitrixExecutiveDashboardAccessLevel = "full" | "domain";

interface BitrixMatchingSessionResponse {
  session_token: string;
  expires_at: string;
  expires_in: number;
  user: BitrixMatchingUser;
}

export interface BitrixReceivablesSessionResponse {
  session_token: string;
  expires_at: string;
  expires_in: number;
  user: BitrixMatchingUser;
  access_level: BitrixReceivablesAccessLevel;
  department_refs: string[];
}

export interface BitrixExecutiveDashboardSessionResponse {
  session_token: string;
  expires_at: string;
  expires_in: number;
  user: BitrixMatchingUser;
  access_level: BitrixExecutiveDashboardAccessLevel;
  roles: string[];
  allowed_blocks: string[];
  allowed_action_domains: string[];
}

interface CachedBitrixSession extends BitrixMatchingSessionResponse {
  cached_at: string;
}

interface CachedBitrixReceivablesSession extends BitrixReceivablesSessionResponse {
  cached_at: string;
}

interface CachedBitrixExecutiveDashboardSession extends BitrixExecutiveDashboardSessionResponse {
  cached_at: string;
}

interface CachedProcurementLabelsSession extends BitrixMatchingSessionResponse {
  cached_at: string;
}

interface CachedProcurementAssortmentSession extends BitrixMatchingSessionResponse {
  cached_at: string;
}

interface CachedProcurementOrderFormationSession extends BitrixMatchingSessionResponse {
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

export function isBitrixReceivablesRoute() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path === "/bitrix/receivables" || path.startsWith("/bitrix/receivables/");
}

export function isBitrixExecutiveDashboardRoute() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path === "/bitrix/executive-dashboard" || path.startsWith("/bitrix/executive-dashboard/");
}

export function isBitrixProcurementLabelsRoute() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path === "/bitrix/procurement-labels" || path.startsWith("/bitrix/procurement-labels/");
}

export function isBitrixProcurementAssortmentRoute() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return path === "/bitrix/procurement-assortment" || path.startsWith("/bitrix/procurement-assortment/");
}

export function isBitrixProcurementOrderFormationRoute() {
  const path = window.location.pathname.replace(/\/+$/, "");
  return (
    path === "/bitrix/procurement-order-formation" ||
    path.startsWith("/bitrix/procurement-order-formation/")
  );
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

function readCachedReceivablesSession(): CachedBitrixReceivablesSession | null {
  try {
    const raw = window.sessionStorage.getItem(RECEIVABLES_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedBitrixReceivablesSession;
    if (Date.parse(cached.expires_at) - Date.now() <= REFRESH_SKEW_MS) {
      window.sessionStorage.removeItem(RECEIVABLES_SESSION_STORAGE_KEY);
      return null;
    }
    return cached;
  } catch {
    return null;
  }
}

function readCachedExecutiveDashboardSession(): CachedBitrixExecutiveDashboardSession | null {
  try {
    const raw = window.sessionStorage.getItem(EXECUTIVE_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedBitrixExecutiveDashboardSession;
    if (Date.parse(cached.expires_at) - Date.now() <= REFRESH_SKEW_MS) {
      window.sessionStorage.removeItem(EXECUTIVE_SESSION_STORAGE_KEY);
      return null;
    }
    return cached;
  } catch {
    return null;
  }
}


function readCachedProcurementLabelsSession(): CachedProcurementLabelsSession | null {
  try {
    const raw = window.sessionStorage.getItem(PROCUREMENT_LABELS_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedProcurementLabelsSession;
    if (Date.parse(cached.expires_at) - Date.now() <= REFRESH_SKEW_MS) {
      window.sessionStorage.removeItem(PROCUREMENT_LABELS_SESSION_STORAGE_KEY);
      return null;
    }
    return cached;
  } catch {
    return null;
  }
}

function readCachedProcurementAssortmentSession(): CachedProcurementAssortmentSession | null {
  try {
    const raw = window.sessionStorage.getItem(PROCUREMENT_ASSORTMENT_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedProcurementAssortmentSession;
    if (Date.parse(cached.expires_at) - Date.now() <= REFRESH_SKEW_MS) {
      window.sessionStorage.removeItem(PROCUREMENT_ASSORTMENT_SESSION_STORAGE_KEY);
      return null;
    }
    return cached;
  } catch {
    return null;
  }
}

function readCachedProcurementOrderFormationSession(): CachedProcurementOrderFormationSession | null {
  try {
    const raw = window.sessionStorage.getItem(PROCUREMENT_ORDER_FORMATION_SESSION_STORAGE_KEY);
    if (!raw) return null;
    const cached = JSON.parse(raw) as CachedProcurementOrderFormationSession;
    if (Date.parse(cached.expires_at) - Date.now() <= REFRESH_SKEW_MS) {
      window.sessionStorage.removeItem(PROCUREMENT_ORDER_FORMATION_SESSION_STORAGE_KEY);
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

function cacheReceivablesSession(session: BitrixReceivablesSessionResponse) {
  const cached: CachedBitrixReceivablesSession = {
    ...session,
    cached_at: new Date().toISOString(),
  };
  try {
    window.sessionStorage.setItem(RECEIVABLES_SESSION_STORAGE_KEY, JSON.stringify(cached));
  } catch {
    // Storage can be restricted in embedded contexts; in-memory axios auth still works.
  }
}

function cacheExecutiveDashboardSession(session: BitrixExecutiveDashboardSessionResponse) {
  const cached: CachedBitrixExecutiveDashboardSession = {
    ...session,
    cached_at: new Date().toISOString(),
  };
  try {
    window.sessionStorage.setItem(EXECUTIVE_SESSION_STORAGE_KEY, JSON.stringify(cached));
  } catch {
    // Storage can be restricted in embedded contexts; in-memory axios auth still works.
  }
}

function cacheProcurementLabelsSession(session: BitrixMatchingSessionResponse) {
  const cached: CachedProcurementLabelsSession = {
    ...session,
    cached_at: new Date().toISOString(),
  };
  try {
    window.sessionStorage.setItem(PROCUREMENT_LABELS_SESSION_STORAGE_KEY, JSON.stringify(cached));
  } catch {
    // Storage can be restricted in embedded contexts; in-memory axios auth still works.
  }
}

function cacheProcurementAssortmentSession(session: BitrixMatchingSessionResponse) {
  const cached: CachedProcurementAssortmentSession = {
    ...session,
    cached_at: new Date().toISOString(),
  };
  try {
    window.sessionStorage.setItem(PROCUREMENT_ASSORTMENT_SESSION_STORAGE_KEY, JSON.stringify(cached));
  } catch {
    // Storage can be restricted in embedded contexts; in-memory axios auth still works.
  }
}

function cacheProcurementOrderFormationSession(session: BitrixMatchingSessionResponse) {
  const cached: CachedProcurementOrderFormationSession = {
    ...session,
    cached_at: new Date().toISOString(),
  };
  try {
    window.sessionStorage.setItem(
      PROCUREMENT_ORDER_FORMATION_SESSION_STORAGE_KEY,
      JSON.stringify(cached)
    );
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

export function resolveBitrixPortalUrl(value?: string | null) {
  const raw = (value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : "";
  } catch {
    // Relative Bitrix detailUrl values need the portal domain from the iframe launch payload.
  }
  if (!raw.startsWith("/")) return "";
  const domain = (window.__MM_BITRIX_LAUNCH__?.domain || "").trim();
  if (!domain) return "";
  try {
    return new URL(raw, `https://${domain}`).toString();
  } catch {
    return "";
  }
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

function receivablesHandlerUrl() {
  return new URL("/bitrix/receivables/", window.location.origin).toString();
}

function executiveDashboardHandlerUrl() {
  return new URL("/bitrix/executive-dashboard/", window.location.origin).toString();
}

function procurementLabelsHandlerUrl() {
  return new URL("/bitrix/procurement-labels/", window.location.origin).toString();
}

function procurementOrderFormationHandlerUrl() {
  return new URL("/bitrix/procurement-order-formation/", window.location.origin).toString();
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

async function ensureBitrixReceivablesLeftMenuPlacement() {
  try {
    if (window.sessionStorage.getItem(RECEIVABLES_LEFT_MENU_STORAGE_KEY) === "1") return;
  } catch {
    // Ignore restricted storage in embedded contexts.
  }

  await loadBitrixSdk();
  await initBitrix();

  const handler = receivablesHandlerUrl();
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
      TITLE: RECEIVABLES_LEFT_MENU_TITLE,
      DESCRIPTION: "Receivables workplace",
      LANG_ALL: {
        ru: {
          TITLE: RECEIVABLES_LEFT_MENU_TITLE,
          DESCRIPTION: "Дебиторка покупателей",
        },
        en: {
          TITLE: "Customer receivables",
          DESCRIPTION: "Customer receivables",
        },
      },
    });
  }

  try {
    window.sessionStorage.setItem(RECEIVABLES_LEFT_MENU_STORAGE_KEY, "1");
  } catch {
    // Storage can be restricted in embedded contexts; binding already succeeded.
  }
}

async function ensureBitrixExecutiveDashboardLeftMenuPlacement() {
  try {
    if (window.sessionStorage.getItem(EXECUTIVE_LEFT_MENU_STORAGE_KEY) === "1") return;
  } catch {
    // Ignore restricted storage in embedded contexts.
  }

  await loadBitrixSdk();
  await initBitrix();

  const handler = executiveDashboardHandlerUrl();
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
      TITLE: EXECUTIVE_LEFT_MENU_TITLE,
      DESCRIPTION: "Executive dashboard",
      LANG_ALL: {
        ru: {
          TITLE: EXECUTIVE_LEFT_MENU_TITLE,
          DESCRIPTION: "Единая управленческая витрина",
        },
        en: {
          TITLE: "Executive dashboard",
          DESCRIPTION: "Executive dashboard",
        },
      },
    });
  }

  try {
    window.sessionStorage.setItem(EXECUTIVE_LEFT_MENU_STORAGE_KEY, "1");
  } catch {
    // Storage can be restricted in embedded contexts; binding already succeeded.
  }
}


async function ensureBitrixProcurementLabelsPlacement() {
  try {
    if (window.sessionStorage.getItem(PROCUREMENT_LABELS_PLACEMENT_STORAGE_KEY) === "1") return;
  } catch {
    // Ignore restricted storage in embedded contexts.
  }

  await loadBitrixSdk();
  await initBitrix();

  const handler = procurementLabelsHandlerUrl();
  const normalizedHandler = normalizeHandlerUrl(handler);
  const placements = await bitrixCall<
    Array<{ placement?: string; handler?: string; title?: string }>
  >("placement.get", {});
  const alreadyBound = placements.some(
    (item) =>
      item.placement === PROCUREMENT_LABELS_DETAIL_PLACEMENT &&
      normalizeHandlerUrl(String(item.handler || "")) === normalizedHandler
  );
  if (!alreadyBound) {
    await bitrixCall<boolean>("placement.bind", {
      PLACEMENT: PROCUREMENT_LABELS_DETAIL_PLACEMENT,
      HANDLER: handler,
      TITLE: PROCUREMENT_LABELS_TITLE,
      DESCRIPTION: "ВЭД этикетки",
      LANG_ALL: {
        ru: {
          TITLE: PROCUREMENT_LABELS_TITLE,
          DESCRIPTION: "ВЭД этикетки",
        },
        en: {
          TITLE: "Generate labels",
          DESCRIPTION: "VED product labels",
        },
      },
    });
  }

  try {
    window.sessionStorage.setItem(PROCUREMENT_LABELS_PLACEMENT_STORAGE_KEY, "1");
  } catch {
    // Storage can be restricted in embedded contexts; binding already succeeded.
  }
}

async function ensureBitrixProcurementOrderFormationPlacement() {
  try {
    if (window.sessionStorage.getItem(PROCUREMENT_ORDER_FORMATION_PLACEMENT_STORAGE_KEY) === "1") {
      return;
    }
  } catch {
    // Ignore restricted storage in embedded contexts.
  }

  await loadBitrixSdk();
  await initBitrix();

  const handler = procurementOrderFormationHandlerUrl();
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
      TITLE: PROCUREMENT_ORDER_FORMATION_MENU_TITLE,
      DESCRIPTION: "Формирование заказов поставщикам",
      LANG_ALL: {
        ru: {
          TITLE: PROCUREMENT_ORDER_FORMATION_MENU_TITLE,
          DESCRIPTION: "Формирование заказов поставщикам",
        },
        en: {
          TITLE: "Supplier order formation",
          DESCRIPTION: "Supplier order formation",
        },
      },
    });
  }

  try {
    window.sessionStorage.setItem(PROCUREMENT_ORDER_FORMATION_PLACEMENT_STORAGE_KEY, "1");
  } catch {
    // Storage can be restricted in embedded contexts; binding already succeeded.
  }
}

function ensureBitrixLeftMenuPlacementInBackground() {
  ensureBitrixLeftMenuPlacement().catch((error: unknown) => {
    console.warn("Не удалось добавить приложение в левое меню Bitrix24", error);
  });
}

function ensureBitrixReceivablesLeftMenuPlacementInBackground() {
  ensureBitrixReceivablesLeftMenuPlacement().catch((error: unknown) => {
    console.warn("Не удалось добавить дебиторку в левое меню Bitrix24", error);
  });
}

function ensureBitrixExecutiveDashboardLeftMenuPlacementInBackground() {
  ensureBitrixExecutiveDashboardLeftMenuPlacement().catch((error: unknown) => {
    console.warn("Не удалось добавить управленческую витрину в левое меню Bitrix24", error);
  });
}

function ensureBitrixProcurementLabelsPlacementInBackground() {
  ensureBitrixProcurementLabelsPlacement().catch((error: unknown) => {
    console.warn("Не удалось добавить вкладку этикеток в Bitrix24", error);
  });
}

function ensureBitrixProcurementOrderFormationPlacementInBackground() {
  ensureBitrixProcurementOrderFormationPlacement().catch((error: unknown) => {
    console.warn("Не удалось добавить формирование заказа в Bitrix24", error);
  });
}

export async function bindBitrixProcurementLabelsPlacement() {
  await ensureBitrixProcurementLabelsPlacement();
}

export async function bindBitrixProcurementAssortmentPlacement() {
  await ensureBitrixProcurementOrderFormationPlacement();
}

export async function bindBitrixProcurementOrderFormationPlacement() {
  await ensureBitrixProcurementOrderFormationPlacement();
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

export async function initializeBitrixReceivablesSession() {
  const cached = readCachedReceivablesSession();
  if (cached) {
    setApiAuthToken(cached.session_token);
    ensureBitrixReceivablesLeftMenuPlacementInBackground();
    return cached;
  }

  clearApiAuthToken();
  let auth = getLaunchAuth();
  if (!auth) {
    await loadBitrixSdk();
    auth = await initBitrix();
  }
  const { data } = await api.post<BitrixReceivablesSessionResponse>("/bitrix/receivables/session", {
    access_token: auth.access_token,
    domain: auth.domain,
    member_id: auth.member_id,
  });
  setApiAuthToken(data.session_token);
  cacheReceivablesSession(data);
  ensureBitrixReceivablesLeftMenuPlacementInBackground();
  return data;
}

export async function initializeBitrixExecutiveDashboardSession() {
  const cached = readCachedExecutiveDashboardSession();
  if (cached) {
    setApiAuthToken(cached.session_token);
    ensureBitrixExecutiveDashboardLeftMenuPlacementInBackground();
    return cached;
  }

  clearApiAuthToken();
  let auth = getLaunchAuth();
  if (!auth) {
    await loadBitrixSdk();
    auth = await initBitrix();
  }
  const { data } = await api.post<BitrixExecutiveDashboardSessionResponse>(
    "/bitrix/executive-dashboard/session",
    {
      access_token: auth.access_token,
      domain: auth.domain,
      member_id: auth.member_id,
    }
  );
  setApiAuthToken(data.session_token);
  cacheExecutiveDashboardSession(data);
  ensureBitrixExecutiveDashboardLeftMenuPlacementInBackground();
  return data;
}

export async function initializeBitrixProcurementLabelsSession() {
  const cached = readCachedProcurementLabelsSession();
  if (cached) {
    setApiAuthToken(cached.session_token);
    ensureBitrixProcurementLabelsPlacementInBackground();
    return cached.user;
  }

  clearApiAuthToken();
  let auth = getLaunchAuth();
  if (!auth) {
    await loadBitrixSdk();
    auth = await initBitrix();
  }
  const { data } = await api.post<BitrixMatchingSessionResponse>("/procurement-labels/session", {
    access_token: auth.access_token,
    domain: auth.domain,
    member_id: auth.member_id,
  });
  setApiAuthToken(data.session_token);
  cacheProcurementLabelsSession(data);
  ensureBitrixProcurementLabelsPlacementInBackground();
  return data.user;
}

export async function initializeBitrixProcurementAssortmentSession() {
  const cached = readCachedProcurementAssortmentSession();
  if (cached) {
    setApiAuthToken(cached.session_token);
    return cached.user;
  }

  clearApiAuthToken();
  let auth = getLaunchAuth();
  if (!auth) {
    await loadBitrixSdk();
    auth = await initBitrix();
  }
  const { data } = await api.post<BitrixMatchingSessionResponse>("/procurement-labels/session", {
    access_token: auth.access_token,
    domain: auth.domain,
    member_id: auth.member_id,
  });
  setApiAuthToken(data.session_token);
  cacheProcurementAssortmentSession(data);
  return data.user;
}

export async function initializeBitrixProcurementOrderFormationSession() {
  const cached = readCachedProcurementOrderFormationSession();
  if (cached) {
    setApiAuthToken(cached.session_token);
    ensureBitrixProcurementOrderFormationPlacementInBackground();
    return cached.user;
  }

  clearApiAuthToken();
  let auth = getLaunchAuth();
  if (!auth) {
    await loadBitrixSdk();
    auth = await initBitrix();
  }
  const { data } = await api.post<BitrixMatchingSessionResponse>(
    "/procurement-order-formation/session",
    {
      access_token: auth.access_token,
      domain: auth.domain,
      member_id: auth.member_id,
    }
  );
  setApiAuthToken(data.session_token);
  cacheProcurementOrderFormationSession(data);
  ensureBitrixProcurementOrderFormationPlacementInBackground();
  return data.user;
}

export function getProcurementLabelsItemId() {
  const launchId = window.__MM_BITRIX_LAUNCH__?.placement_options?.ID;
  if (typeof launchId === "number" || typeof launchId === "string") {
    const value = String(launchId).trim();
    if (value) return value;
  }
  const url = new URL(window.location.href);
  return url.searchParams.get("itemId") || "";
}

export function getProcurementAssortmentItemId() {
  return getProcurementLabelsItemId();
}
