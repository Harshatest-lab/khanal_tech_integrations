# Dev migration — Lambda out of VPC (remove NAT / endpoints)

**Goal:** Stop paying for **NAT Gateway**, **SSM VPC endpoint**, and **unused Elastic IPs** in **dev**, while keeping **RDS**, **Lambda**, **SSM Parameter Store**, S3, and API Gateway working.

**Account:** `699013266218` (dev)  
**Region:** `ap-south-1` (Mumbai)  
**Stage:** `dev` (`delivery-api` Serverless stage)

Do the steps **in order**. Do **not** delete NAT or VPC endpoints until **Step 5** passes.

**Related docs:** [INFRA_AWS_FROM_SCRATCH.md](./INFRA_AWS_FROM_SCRATCH.md) (what you built originally), [LAMBDA_VPC_SETUP.md](./LAMBDA_VPC_SETUP.md) (old VPC pattern — deprecated after this migration).

---

## What changes (summary)

| Component | Before | After |
|-----------|--------|-------|
| **Lambda** (`delhiveryWebhookWorker`) | Inside custom VPC private subnets | **No VPC** (default Lambda networking) |
| **RDS** | In custom VPC, public access | **Unchanged** (stays in VPC; public endpoint) |
| **SSM Parameter Store** (`/sap/*`) | Used via VPC endpoint or NAT | **Unchanged** (free; Lambda calls AWS API directly) |
| **NAT Gateway** | Running (~$58/mo) | **Deleted** (Step 6) |
| **SSM VPC endpoint** | Running (~$13/mo) | **Deleted** (Step 6) |
| **Elastic IP** (NAT) | Allocated | **Released** (Step 6) |
| **Default VPC** | Untouched | **Keep** (free) |
| **Custom VPC** (`khanal-*-vpc`) | NAT + Lambda subnets + RDS | **Keep for RDS only** (VPC itself is free) |

**Expected dev savings:** ~**$75–80/month** (NAT + SSM endpoint + idle/in-use NAT IPv4).

---

## Step 1 — Inventory (console, read-only)

**You are here — start here.** Do not change anything yet.

Sign in to AWS account **`699013266218`**, region **`ap-south-1`**.

Write down these values (search by **Name** tag in the console):

| What | Where in console | Your value (fill in) |
|------|------------------|----------------------|
| Custom VPC | VPC → Your VPCs | Name: `khanal-prod-vpc` or `khanal-dev-vpc` → VPC ID: |
| RDS instance | RDS → Databases | Identifier: → Endpoint: |
| RDS publicly accessible? | RDS → DB → Connectivity | Yes / No: |
| RDS security group | RDS → Connectivity → VPC security groups | SG ID + name: |
| NAT Gateway | VPC → NAT gateways | Name `khanal-*-nat-gw` → ID: |
| Elastic IP (NAT) | EC2 → Elastic IPs | Associated with NAT? → Allocation ID: |
| SSM VPC endpoint | VPC → Endpoints | `com.amazonaws.ap-south-1.ssm` Interface → ID: |
| S3 Gateway endpoint | VPC → Endpoints | `com.amazonaws.ap-south-1.s3` Gateway → ID: |
| Lambda function | Lambda → Functions | `delivery-api-dev-delhiveryWebhookWorker` |
| Lambda in VPC? | Lambda → Configuration → VPC | Subnets + SG listed? |
| SSM parameters | Systems Manager → Parameter Store | `/sap/sl_config`, `/sap/credentials`, `/sap/b1_session` exist? |
| Current `DATABASE_URL` host | GitHub secret or local `.env` | Same as RDS endpoint? |

**Checkpoint:** You know RDS endpoint, RDS SG, and that Lambda is currently in VPC. If RDS **Public access = No**, note it — you must turn it **Yes** before Lambda can connect without VPC (Step 4).

---

## Step 2 — Repo: remove VPC from Serverless (dev deploy)

Edit the repo so the next deploy puts Lambda **outside** the VPC.

### 2a — `services/delivery-api/serverless.yml`

Under `functions.delhiveryWebhookWorker`, **remove** the entire `vpc:` block:

```yaml
    vpc:
      securityGroupIds:
        - ${env:LAMBDA_VPC_SECURITY_GROUP_ID}
      subnetIds:
        - ${env:LAMBDA_VPC_SUBNET_ID_1}
        - ${env:LAMBDA_VPC_SUBNET_ID_2}
```

Update the comment above it from “SQS → VPC → RDS” to “SQS → RDS (Lambda not in VPC)”.

### 2b — `services/B2C_Invoice/serverless.yml` (only if you deploy this service in dev)

Remove `vpc:` from `walmartSync` and `walmartDashboardApi`.

### 2c — `.github/workflows/deploy.yml`

Remove these env vars from the deploy job:

- `LAMBDA_VPC_SECURITY_GROUP_ID`
- `LAMBDA_VPC_SUBNET_ID_1`
- `LAMBDA_VPC_SUBNET_ID_2`

### 2d — `.github/workflows/ci.yml`

Remove the three `LAMBDA_VPC_*` placeholder env vars from the `serverless package` step.

### 2e — `services/delivery-api/.env.example`

Remove `LAMBDA_VPC_SECURITY_GROUP_ID`, `LAMBDA_VPC_SUBNET_ID_1`, `LAMBDA_VPC_SUBNET_ID_2` lines.

**No Python code changes** — `DATABASE_URL`, SSM, S3, and SAP logic stay the same.

**Checkpoint:** Commit or stash these changes; proceed to deploy in Step 3.

---

## Step 3 — Deploy Lambda without VPC (dev)

From your laptop (or GitHub Actions **Deploy** workflow with `stage: dev`):

```bash
bash scripts/build_shared_layer.sh
cd services/delivery-api
npm install
```

Set env (use your real values; **do not** set `LAMBDA_VPC_*`):

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@RDS_ENDPOINT:5432/khanal_serverless"
export DELHIVERY_WEBHOOK_SECRET="..."
export POD_S3_BUCKET="delivery-api-dev-delhivery-epod"   # or your bucket name
export SAP_SL_USE_SSM=1                                     # if you use SSM for SAP
export AWS_REGION=ap-south-1

npx serverless deploy --stage dev --region ap-south-1
```

**Verify in console:**

1. **Lambda** → `delivery-api-dev-delhiveryWebhookWorker` → **Configuration** → **VPC** → should show **No VPC configuration** (or empty).
2. **CloudFormation** → stack `delivery-api-dev` → update completed successfully.

**Checkpoint:** Lambda is deployed and **not** in a VPC. NAT and endpoints can still exist — that is OK for now.

---

## Step 4 — RDS security group (allow Lambda without VPC)

Lambda outside VPC has **no security group** and **no fixed IP**. RDS must accept connections on its **public endpoint**.

### 4a — Confirm RDS public access

1. **RDS** → your database → **Modify** (if needed).
2. **Connectivity** → **Public access** → **Yes**.
3. Apply (may need a short maintenance window).

### 4b — Update RDS security group inbound rules

**EC2** → **Security Groups** → open **`khanal-*-sg-rds-postgres`** (or whatever RDS uses).

| Type | Port | Source | Purpose |
|------|------|--------|---------|
| PostgreSQL | 5432 | **Your IP**/32 | `psql`, DBeaver, dashboard |
| PostgreSQL | 5432 | **0.0.0.0/0** | Lambda without VPC (**dev only**) |

**Remove** (if present): inbound rule with source = **`sg-delivery-api-lambda`** (only worked for VPC Lambda).

**Optional (recommended):** append `?sslmode=require` to `DATABASE_URL`.

**Checkpoint:** From your laptop:

```bash
psql "$DATABASE_URL" -c "SELECT 1;"
```

---

## Step 5 — Validate (before deleting NAT or endpoints)

Run **all** checks. If any fail, **stop** — do not go to Step 6.

| # | Test | How | Pass? |
|---|------|-----|-------|
| 1 | Laptop → RDS | `psql "$DATABASE_URL" -c "SELECT 1;"` | ☐ |
| 2 | Lambda → RDS | POST Delhivery webhook (or enqueue SQS test message); check `delhivery_lr` | ☐ |
| 3 | Lambda → S3 | POD webhook with base64 EPOD; object in `POD_S3_BUCKET` | ☐ |
| 4 | Lambda → SSM | If `SAP_SL_USE_SSM=1`, trigger SAP path; no SSM timeout in CloudWatch | ☐ |
| 5 | Lambda → SAP | Shipment/POD SAP PATCH succeeds (if SAP enabled) | ☐ |
| 6 | Cold start | CloudWatch `REPORT` — `Init Duration` should be lower than VPC era | ☐ |

**CloudWatch:** Log group `/aws/lambda/delivery-api-dev-delhiveryWebhookWorker`.

**SAP firewall:** If SAP only whitelisted the **old NAT Elastic IP**, update SAP firewall or confirm SAP is reachable without IP lock.

**Checkpoint:** All critical paths work with NAT **still running**. Safe to remove paid resources.

---

## Step 6 — Delete paid VPC resources (dev only)

Delete **in this order**:

### 6a — SSM VPC Interface endpoint

1. **VPC** → **Endpoints**.
2. Select endpoint for **`com.amazonaws.ap-south-1.ssm`** (Name e.g. `khanal-*-vpce-ssm`).
3. **Actions** → **Delete VPC endpoints**.

Saves ~**$13/month**. **SSM Parameter Store is not deleted** — only the private network shortcut.

### 6b — S3 Gateway endpoint (optional)

1. **VPC** → **Endpoints** → S3 Gateway (`khanal-*-vpce-s3`).
2. Delete if present (was free; not needed after migration).

### 6c — NAT Gateway

1. **VPC** → **NAT gateways**.
2. Select **`khanal-*-nat-gw`** → **Actions** → **Delete NAT gateway**.
3. Wait until status is **Deleted**.

Saves ~**$58/month**.

### 6d — Release Elastic IP

1. **EC2** → **Elastic IPs**.
2. Select the IP that was attached to NAT (tag e.g. `khanal-*-nat-eip`).
3. **Actions** → **Release Elastic IP address**.

Saves ~**$3.60/month** if it was idle/unattached.

### 6e — Optional cleanup (free resources, reduces clutter)

Safe to delete if nothing else uses them:

- Subnets `khanal-*-lambda-nat-1a`, `khanal-*-lambda-nat-1b`
- Route table `khanal-*-rt-lambda-nat`
- Security group `khanal-*-sg-delivery-api-lambda`
- Security group `khanal-*-sg-vpce-ssm`

**Do not delete:**

- Custom VPC (RDS still inside)
- RDS subnets (`khanal-*-rds-1a`, `khanal-*-rds-1b`)
- Internet gateway (if RDS public access uses it)
- RDS instance
- **Default VPC**

**Checkpoint:** VPC → NAT gateways = empty. No Interface endpoint for SSM. Elastic IP released.

---

## Step 7 — GitHub secrets cleanup

**Settings** → **Secrets and variables** → **Actions** — **remove** (dev deploy no longer needs them):

- `LAMBDA_VPC_SECURITY_GROUP_ID`
- `LAMBDA_VPC_SUBNET_ID_1`
- `LAMBDA_VPC_SUBNET_ID_2`

**Keep:**

- `DATABASE_URL`, `AWS_ROLE_ARN`, `AWS_REGION`
- `DELHIVERY_WEBHOOK_SECRET`, `POD_S3_BUCKET`, `POD_S3_PREFIX`
- `SAP_SL_USE_SSM`, `SAP_SL_*`, `SAP_SL_VERIFY_SSL`

---

## Step 8 — Confirm billing (next month)

**Billing** → **Bills** — these lines should **drop** for Mumbai:

| Line item | Was ~ |
|-----------|--------|
| EC2 → RegionalNatGateway | $58 |
| VPC → VpcEndpoint | $13 |
| VPC → Public IPv4 (idle NAT EIP) | $3 |

**Should remain:**

| Line item | ~ |
|-----------|---|
| RDS | $12 |
| VPC → Public IPv4 (RDS public IP) | $4 |
| Lambda / API GW / SQS | low or $0 |

Also check **Hyderabad** (`ap-south-2`) if you see ~**$3.60** Public IPv4 — unrelated to Mumbai stack; release stray EIPs there if unused.

---

## Rollback (if something breaks before Step 6)

1. Re-add `vpc:` block to `serverless.yml` with original subnet + SG IDs.
2. Set `LAMBDA_VPC_*` env vars and redeploy.
3. Restore RDS SG rule: PostgreSQL 5432 from `sg-delivery-api-lambda`.
4. NAT and endpoints still exist if you have not completed Step 6.

---

## After dev — live account

Repeat the same steps on the **live** AWS account when ready. Use stricter RDS SG on live (avoid `0.0.0.0/0` if policy requires — plan IP allowlisting or another pattern).

---

## One-page checklist

```
Step 1  ☐ Inventory (RDS endpoint, SG, NAT, endpoints, Lambda in VPC)
Step 2  ☐ Repo: remove vpc from serverless.yml + workflows + .env.example
Step 3  ☐ Deploy delivery-api --stage dev (no LAMBDA_VPC_* vars)
Step 4  ☐ RDS public access Yes; SG 5432 from your IP + 0.0.0.0/0 (dev)
Step 5  ☐ Validate psql, webhook, S3, SSM, SAP
Step 6  ☐ Delete SSM endpoint → NAT → release EIP → optional cleanup
Step 7  ☐ Remove LAMBDA_VPC_* GitHub secrets
Step 8  ☐ Confirm next bill (no NAT / VpcEndpoint)
```

**Keep using:** SSM Parameter Store (`/sap/*`), RDS, Lambda, S3, API Gateway, SQS.
