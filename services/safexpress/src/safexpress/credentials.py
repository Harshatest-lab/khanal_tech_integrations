"""Runtime credentials: DATABASE_URL from env, SafeExpress auth from SSM.

SafeExpress credentials live in one SecureString JSON parameter (default
``/safexpress/credentials``)::

    {
      "login_url": "...",
      "username": "...",
      "password": "...",
      "client_basic_auth": "...",   # the OAuth client's Basic auth header value
      "api_key": "...",             # x-api-key sent on POD fetches
      "identifier": "...",          # Identifier header sent on POD fetches
      "pod_url": "..."              # base URL for POD document fetch, waybill appended
    }

The legacy Frappe integration had this same client_basic_auth value
hardcoded directly in source
(khanal_tech_integrations/utils/safexpress2/auth.py) — moving it to SSM
here is a deliberate fix, not a preserved pattern. Populate this parameter
with the real value from that file when setting up SSM for this service;
never commit it to git.

Token is cached separately in ``/safexpress/token`` (SecureString JSON:
``{"access_token": ..., "expires_on": "<ISO8601>"}``), renewed in-process
when expired. No cross-invocation lock (unlike SAP SL's session cache) —
deliberate simplification given SafeExpress's low invocation volume; a
double-renewal on rare concurrent cold starts is harmless (SafeExpress's
own API just issues a new token).
"""

from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


DATABASE_URL = _env("DATABASE_URL")
SAFEXPRESS_CREDENTIALS_PARAM = _env("SAFEXPRESS_CREDENTIALS_PARAM", "/safexpress/credentials")
SAFEXPRESS_TOKEN_PARAM = _env("SAFEXPRESS_TOKEN_PARAM", "/safexpress/token")

try:
    from safexpress.credentials_local import DATABASE_URL as _LOCAL_DATABASE_URL  # type: ignore[import-not-found]

    if _LOCAL_DATABASE_URL:
        DATABASE_URL = _LOCAL_DATABASE_URL.strip()
except ImportError:
    pass
