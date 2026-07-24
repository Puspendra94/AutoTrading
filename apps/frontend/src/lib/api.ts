// Shared REST client: attaches the JWT, handles JSON, and redirects to /login on 401.
// Every page's script imports from here instead of calling fetch() directly.

// Same-origin `/api` by default: the browser always talks to the frontend's own origin,
// which proxies `/api/*` to the backend (vite dev proxy locally; server.mjs in Docker).
// This is what lets the backend run on a fixed internal port while its published host
// port is random — the browser never needs to know it. Set PUBLIC_API_URL to point the
// browser straight at a backend URL instead (e.g. for a split deploy).
export const API_URL: string = (import.meta.env.PUBLIC_API_URL as string) || '/api';

const TOKEN_KEY = 'jwt';

export class ApiError extends Error {
  status: number;
  body: any;
  constructor(status: number, body: any) {
    const message =
      (body && typeof body === 'object' && (body.message || body.error)) ||
      (typeof body === 'string' && body) ||
      `Request failed with status ${status}`;
    super(Array.isArray(message) ? message.join(', ') : message);
    this.status = status;
    this.body = body;
  }
}

export function getToken(): string | null {
  if (typeof localStorage === 'undefined') return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export function logout() {
  clearToken();
  window.location.href = '/login';
}

/** Call at the top of every protected page's script. Redirects to /login if no JWT is stored. */
export function requireAuth(): string | null {
  const token = getToken();
  if (!token) {
    window.location.href = '/login';
    return null;
  }
  return token;
}

function safeJsonParse(text: string) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new ApiError(0, { message: 'Network error — is the backend running?' });
  }

  if (res.status === 401) {
    clearToken();
    if (window.location.pathname !== '/login') {
      window.location.href = '/login';
    }
    throw new ApiError(401, { message: 'Session expired, please log in again' });
  }

  const text = await res.text();
  const data = text ? safeJsonParse(text) : null;

  if (!res.ok) {
    throw new ApiError(res.status, data);
  }
  return data as T;
}

export const apiGet = <T = any>(path: string) => request<T>('GET', path);
export const apiPost = <T = any>(path: string, body?: unknown) => request<T>('POST', path, body);
export const apiPatch = <T = any>(path: string, body?: unknown) => request<T>('PATCH', path, body);
export const apiDelete = <T = any>(path: string) => request<T>('DELETE', path);
