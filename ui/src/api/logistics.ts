import type { AxiosRequestConfig } from "axios";

import { isBitrixLogisticsRoute, refreshBitrixLogisticsSession } from "./bitrix";
import { api } from "./client";

type LogisticsRetryOptions = {
  refreshSession?: () => Promise<unknown>;
  isBitrixRoute?: () => boolean;
};

function responseStatus(error: unknown) {
  return typeof error === "object" && error !== null && "response" in error
    ? (error as { response?: { status?: number } }).response?.status
    : undefined;
}

export async function withLogisticsAuthRetry<T>(
  request: () => Promise<T>,
  options: LogisticsRetryOptions = {}
) {
  const isBitrixRoute = options.isBitrixRoute || isBitrixLogisticsRoute;
  const refreshSession = options.refreshSession || refreshBitrixLogisticsSession;
  try {
    return await request();
  } catch (error: unknown) {
    if (responseStatus(error) !== 401 || !isBitrixRoute()) throw error;
    await refreshSession();
    return request();
  }
}

export const logisticsApi = {
  get<T>(path: string, config?: AxiosRequestConfig) {
    return withLogisticsAuthRetry(() => api.get<T>(path, config));
  },
  post<T>(path: string, data?: unknown, config?: AxiosRequestConfig) {
    return withLogisticsAuthRetry(() => api.post<T>(path, data, config));
  },
};
