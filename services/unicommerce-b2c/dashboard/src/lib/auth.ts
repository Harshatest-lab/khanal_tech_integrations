// Auth: Cognito User Pool JWT authorizer on the HTTP API directly (no
// backend login proxy) — calls Cognito's IdP endpoint straight, same
// pattern as the vanilla-JS dashboard this replaces.
export const COGNITO_REGION = "ap-south-1";
export const COGNITO_CLIENT_ID = "5bvg6pblh922ldd5n065vvpf84";
const COGNITO_IDP_URL = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/`;

const AUTH_STORAGE_KEY = "unicommerce_b2c_dashboard_auth";

export interface AuthSession {
  id_token: string;
  access_token: string;
  refresh_token?: string;
}

let authSession: AuthSession | null = null;

function loadAuthSession(): AuthSession | null {
  try {
    const raw = sessionStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && parsed.id_token ? parsed : null;
  } catch {
    return null;
  }
}

function saveAuthSession(session: AuthSession) {
  authSession = session;
  sessionStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
}

export function clearAuthSession() {
  authSession = null;
  sessionStorage.removeItem(AUTH_STORAGE_KEY);
}

function jwtExpMs(token: string): number {
  try {
    const part = token.split(".")[1];
    if (!part) return 0;
    const payload = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/")));
    return payload.exp ? payload.exp * 1000 : 0;
  } catch {
    return 0;
  }
}

function isSessionValid(session: AuthSession | null): boolean {
  if (!session?.id_token) return false;
  const exp = jwtExpMs(session.id_token);
  if (!exp) return true;
  return Date.now() < exp - 60_000;
}

export function getAuthSession(): AuthSession | null {
  return authSession;
}

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers = { ...(extra || {}) };
  if (authSession?.id_token) {
    headers.Authorization = `Bearer ${authSession.id_token}`;
  }
  return headers;
}

async function cognitoRequest(target: string, body: Record<string, unknown>) {
  const res = await fetch(COGNITO_IDP_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = data?.message || data?.__type || `${res.status} Cognito error`;
    throw new Error(message);
  }
  return data;
}

export async function loginWithPassword(email: string, password: string): Promise<void> {
  const data = await cognitoRequest("InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: COGNITO_CLIENT_ID,
    AuthParameters: { USERNAME: email, PASSWORD: password },
  });
  if (data.ChallengeName) {
    throw new Error(`Login requires extra step: ${data.ChallengeName} (contact admin)`);
  }
  const result = data.AuthenticationResult;
  if (!result?.IdToken) throw new Error("Cognito did not return a token");
  saveAuthSession({
    id_token: result.IdToken,
    access_token: result.AccessToken,
    refresh_token: result.RefreshToken,
  });
}

export async function refreshAuthSession(): Promise<boolean> {
  if (!authSession?.refresh_token) return false;
  try {
    const data = await cognitoRequest("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: COGNITO_CLIENT_ID,
      AuthParameters: { REFRESH_TOKEN: authSession.refresh_token },
    });
    const result = data.AuthenticationResult;
    if (!result?.IdToken) return false;
    saveAuthSession({
      id_token: result.IdToken,
      access_token: result.AccessToken,
      refresh_token: authSession.refresh_token,
    });
    return true;
  } catch {
    return false;
  }
}

export async function ensureAuthenticated(): Promise<boolean> {
  authSession = loadAuthSession();
  if (isSessionValid(authSession)) return true;
  if (authSession?.refresh_token && (await refreshAuthSession())) return true;
  clearAuthSession();
  return false;
}

export async function handleUnauthorized(): Promise<boolean> {
  if (authSession?.refresh_token && (await refreshAuthSession())) return true;
  clearAuthSession();
  return false;
}
