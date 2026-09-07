# B2B sales orders (Blinkit)

```
src/handlers.py     Lambda entry
src/blinkit.py      IMAP + parse + RDS/S3
src/b2b_sappost.py  SAP Orders (all channels)
src/store.py        Postgres
dashboard/          UI
```

## CardCode vs salesperson

- **CardCode** `C03570` is hardcoded in `blinkit.py` (`BLINKIT_CARD_CODE`). Not SSM.
- **SalesPersonCode** comes from `b2b.users.sap_salesperson_code` for the person who clicks Post to SAP.

## Tables

Tables live in schema **`b2b`** on the same database as Walmart (`public.walmart_*` is unchanged).

- **New DB:** apply `sql/b2b_so.sql`
- **Existing khanaltestdb** (tables currently in `public`): apply `sql/b2b_so_move_to_schema.sql` once, then `\dt b2b.*`

Seed users (Cognito email must match):

```sql
INSERT INTO b2b.users (email, full_name, role, sap_salesperson_code)
VALUES
  ('ops1@khanalfoods.com', 'Ops One', 'admin', '103'),
  ('ops2@khanalfoods.com', 'Ops Two', 'reviewer', '104');
```

Create the same emails in Cognito. Role in **RDS** is what the API checks.

## SSM (only these)

- `/b2b/imap` — one shared mailbox; each channel uses a Gmail **label** under `folders.{channel}` plus optional `card_codes.{channel}` for SAP Business Partner lookup
- `/sap/sl_config`, `/sap/credentials`, `/sap/b1_session` — **Orders post only** (same test SAP as delivery-api)
- `/sap/hana_config`, `/sap/hana_credentials` — fetch lookups (OCRD / CRD1 / OITM). Set `HANA_USE_SSM=1`.

Example `/b2b/imap`:

```json
{
  "email": "ops@example.com",
  "password": "app-password",
  "imap_server": "imap.gmail.com",
  "imap_port": 993,
  "folders": {
    "blinkit": "Blinkit",
    "zepto": "Zepto",
    "instamart": "Instamart",
    "bigbasket": "Bigbasket"
  },
  "card_codes": {
    "blinkit": "C03570"
  }
}
```

Dashboard **Fetch** runs per channel (passes `channel` to Lambda). Legacy top-level `"folder": "Blinkit"` still works for Blinkit-only setups.

## Scheduled fetch

Lambda **`b2bFetch`** (`b2b-so-<stage>-b2bFetch`) runs IMAP intake on a schedule via EventBridge Scheduler (`Asia/Kolkata`):

| Time (IST) | Channels |
|---|---|
| 10:00 | blinkit, zepto, instamart, bigbasket |
| 12:00 | blinkit, zepto, instamart, bigbasket |
| 16:30 | blinkit, zepto, instamart, bigbasket |

Each run passes `{"channel": "...", "trigger": "schedule"}`. Manual dashboard Fetch still works the same way.

No `/b2b/blinkit/customer` param.

Also: `DATABASE_URL`, SES from/to emails, seed SKU + ship-to maps.
