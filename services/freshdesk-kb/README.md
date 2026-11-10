# freshdesk-kb (separate AWS stack)

Read-only middleware between the **Shopify help chat panel** (dogseeindia theme,
`snippets/kb-chat.liquid` + `assets/kb-chat.js`) and the **Freshdesk knowledge base**
(Solutions). The browser never sees the Freshdesk API key; this Lambda holds it.

"Talk to a human" in the panel opens the Freshdesk web chat widget directly — that
part does not go through this service.

| Item | Location |
|------|----------|
| Stack name | `freshdesk-kb-<stage>` |
| HTTP route | `GET /v1/freshdesk/kb` |
| Python entry | `src/handlers.py` → `kb_handler` → `freshdesk_kb/api.py` |
| Credentials | SSM SecureString `/freshdesk/credentials` |

## API

| Request | Response |
|---------|----------|
| `?action=index` | `{"folders": [{"id", "name", "articles": [{"id", "title"}]}]}` |
| `?action=article&id=<id>` | `{"id", "title", "html"}` (404 if not published/public) |
| `?action=search&q=<text>` | `{"results": [{"id", "title"}]}` (top 5 keyword matches) |

Only **published** articles (`status = 2`) in folders with visibility **All users**
(`visibility = 1`) under the configured categories are ever returned. The whole
knowledge base is cached in memory for 60 minutes per container
(`FRESHDESK_KB_CACHE_MINUTES`), so article edits show up on the site within an hour.

## SSM parameter (one-time)

```bash
aws ssm put-parameter --region ap-south-1 --type SecureString \
  --name /freshdesk/credentials \
  --value '{"domain":"khanalfoods.freshdesk.com","api_key":"<FRESHDESK_API_KEY>","category_ids":[<CATEGORY_ID>]}'
```

To show another category later, update `category_ids` (add `--overwrite`); no redeploy
needed — new containers pick it up.

## Deploy

From repo root:

```bash
bash scripts/build_shared_layer.sh
cd services/freshdesk-kb
npm install
npx serverless deploy --stage dev      # QA store origin
npx serverless deploy --stage prod     # dogseechew.com origins
```

Or GitHub Actions → **Deploy** → service `freshdesk-kb`.

Allowed browser origins per stage are in `serverless.yml` → `custom.allowedOriginsByStage`.

## Connect the theme

Copy the deployed endpoint (`https://<api-id>.execute-api.ap-south-1.amazonaws.com/v1/freshdesk/kb`)
into Shopify **Theme settings → Freshdesk chat → Knowledge base API URL**.

Quick check: open `<endpoint>?action=index` in a browser.

| Response | Meaning |
|----------|---------|
| `{"folders":[...]}` | Working |
| `{"folders":[]}` | Connected, but nothing published in an "All users" folder |
| `{"error":"Not configured"}` | SSM parameter missing or incomplete |
| `{"error":"Knowledge base unavailable"}` | Freshdesk rejected the call — check CloudWatch logs (wrong key/domain/category) |
