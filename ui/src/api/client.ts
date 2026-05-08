import axios from "axios";

const apiBase = import.meta.env.VITE_API_BASE_URL || "/api";

export const api = axios.create({
  baseURL: apiBase,
});

export function setApiAuthToken(token: string) {
  api.defaults.headers.common.Authorization = `Bearer ${token}`;
}

export function clearApiAuthToken() {
  delete api.defaults.headers.common.Authorization;
}
