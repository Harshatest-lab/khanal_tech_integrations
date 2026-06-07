# Production AWS setup — simple steps with names

**Region:** **Asia Pacific (Mumbai)** — **`ap-south-1`**. Use this region for every resource in this guide.

**Naming:** everything starts with **`khanal-prod-`** so you can search it in the console.

Do the steps **in order**. Skip GitHub (Step 20–21) if you deploy only from your laptop.

---

## Step 1 — VPC

1. **VPC** → **Your VPCs** → **Create VPC** → **VPC only**.
2. **Name tag:** `khanal-prod-vpc`
3. **IPv4 CIDR:** `10.0.0.0/16`
4. Create → save the **VPC ID** (you need it for everything below).
5. **If RDS will use “Public access = Yes”** (later in Step 8), turn on DNS on this VPC **now** (otherwise RDS create fails with *“VPC does not support DNS resolution, DNS hostnames, or both”*):
   - Select **`khanal-prod-vpc`** → **Actions** → **Edit VPC settings**.
   - Turn **on**: **Enable DNS resolution** and **Enable DNS hostnames** → **Save**.

---

## Step 2 — Internet Gateway

1. **VPC** → **Internet gateways** → **Create internet gateway**.
2. **Name:** `khanal-prod-igw`
3. Create → **Attach to VPC** → choose **`khanal-prod-vpc`**.

---

## Step 3 — Subnets (create 6 subnets, all in `khanal-prod-vpc`)

Use **VPC** → **Subnets** → **Create subnet** six times:

| # | Name (Name tag) | AZ | IPv4 CIDR |
|---|-----------------|-----|-----------|
| 1 | `khanal-prod-public-1a` | ap-south-1a | 10.0.0.0/24 |
| 2 | `khanal-prod-public-1b` | ap-south-1b | 10.0.1.0/24 |
| 3 | `khanal-prod-lambda-nat-1a` | ap-south-1a | 10.0.10.0/24 |
| 4 | `khanal-prod-lambda-nat-1b` | ap-south-1b | 10.0.11.0/24 |
| 5 | `khanal-prod-rds-1a` | ap-south-1a | 10.0.20.0/24 |
| 6 | `khanal-prod-rds-1b` | ap-south-1b | 10.0.21.0/24 |

After creating them:

- Open **`khanal-prod-public-1a`** and **`khanal-prod-public-1b`** → **Edit subnet settings** → turn **Auto-assign public IPv4** **On** → Save.

---

## Step 4 — Route table for internet (IGW)

1. **VPC** → **Route tables** → **Create route table**.
2. **Name:** `khanal-prod-rt-public`
3. **VPC:** `khanal-prod-vpc`
4. Open **`khanal-prod-rt-public`** → **Routes** → **Edit routes**:
   - Add: **Destination** `0.0.0.0/0` → **Target** → **Internet Gateway**.
   - In the gateway field, **do not type anything by hand** (no names, no example IDs). Click the box and **choose the row** whose **Name** column shows **`khanal-prod-igw`**. AWS will fill in **your** gateway ID automatically (format: `igw-` plus 17 hexadecimal characters — **different in every account**; copying an example from a doc will fail with “does not exist”).

**Why the error:** Route targets must be **resource IDs** (`igw-xxxxxxxx`), not **Name tags**. Same rule later for **NAT** (`nat-xxxxxxxx`).
5. **Subnet associations** → associate these 4 subnets:
   - `khanal-prod-public-1a`
   - `khanal-prod-public-1b`
   - `khanal-prod-rds-1a`
   - `khanal-prod-rds-1b`

*(If you want RDS fully private with no internet, do not put the two `rds` subnets here — use a separate doc or ask; this path is for “RDS can be public + SG locked”.)*

---

## Step 5 — Elastic IP + NAT Gateway

### 5a — Elastic IP (EIP)

1. **EC2** → **Elastic IPs** → **Allocate Elastic IP address**.
2. **Public IPv4 address pool:** **Amazon’s pool of IPv4 addresses** (default).
3. **Network border group:** leave **`ap-south-1`** (Mumbai region). If the console shows a dropdown:
   - Choose **`ap-south-1`** or **Use: "ap-south-1"`** — the **regional** Mumbai pool.  
   - Do **not** worry about the longer line like `ap-south-1 (ap-south-1a, ap-south-1b, …)` unless AWS support or your network team asked for a specific border group; for a **NAT Gateway in `ap-south-1`**, the simple **`ap-south-1`** value is correct.
4. (Optional) **Add tag** → Key **`Name`**, Value **`khanal-prod-nat-eip`**.
5. **Allocate** → wait until it shows in the list.

### 5b — NAT Gateway

1. **VPC** → **NAT gateways** → **Create NAT gateway**.
2. **Name:** `khanal-prod-nat-gw`
3. **VPC:** select **`khanal-prod-vpc`** only (the VPC from Step 1 — name tag **`khanal-prod-vpc`**). **Do not** choose **`default`** or any other VPC.
4. **Subnet / Availability Zone:** if the console asks for a subnet or AZ, choose subnet **`khanal-prod-public-1a`** (Mumbai **`ap-south-1a`**). If you see **“Regional”** NAT with no subnet, still ensure the NAT is created **inside `khanal-prod-vpc`** (same VPC as your public subnet and NAT EIP).
5. **Connectivity type:** **Public** (if shown).
6. **Elastic IP:** if the console offers **“Automatic”** vs **manual** allocation, prefer **manually** selecting the EIP you created in **5a** (tag **`khanal-prod-nat-eip`**) so outbound IP stays stable for SAP/firewall rules. If you only see Automatic, proceed; you can still use the NAT for Lambda.
7. **Create NAT gateway** → wait until status is **Available**.

---

## Step 6 — Route table for Lambda (NAT)

1. **Route tables** → **Create route table**.
2. **Name:** `khanal-prod-rt-lambda-nat`
3. **VPC:** `khanal-prod-vpc`
4. **Routes** → add **0.0.0.0/0** → **NAT Gateway** → **choose from the list only** the NAT named **`khanal-prod-nat-gw`** (do not paste an example `nat-…` ID from documentation).
5. **Subnet associations** → associate **only** these 2 subnets:
   - `khanal-prod-lambda-nat-1a`
   - `khanal-prod-lambda-nat-1b`

Do **not** put the RDS subnets on this table.

---

## Step 7 — Security groups (3 groups)

### What a security group is (simple)

A **security group** is a **virtual firewall** attached to **network interfaces** in your VPC. It answers:

- **Who may send traffic *in*?** (inbound rules)  
- **Where may this resource send traffic *out*?** (outbound rules — often “anywhere” for Lambda)

You need **three** groups because **three different roles** talk to the network in different ways: **Lambda**, **RDS**, and the **SSM VPC endpoint**. Each gets its own rules so you do not open the database to the whole internet by mistake.

| Group | Attached to | What it protects |
|--------|-------------|------------------|
| **`khanal-prod-sg-delivery-api-lambda`** | **Lambda** ENIs (when you deploy `delhiveryWebhookWorker` in the VPC) | Only needed inbound for **HTTPS** (e.g. to reach **VPC interface endpoints** like SSM). Lambda also talks **out** to RDS, SAP, S3. |
| **`khanal-prod-sg-rds-postgres`** | **RDS** instance | Only **Postgres 5432** from **Lambda’s security group** (and optionally **your IP** for `psql`/dashboard). Nothing else reaches the DB. |
| **`khanal-prod-sg-vpce-ssm`** | **SSM interface endpoint** ENIs | Only **HTTPS 443** from **Lambda’s security group** so Lambda can call **Parameter Store** (`/sap/...`) without opening the endpoint to the world. |

**Why not one security group for everything?**  
Best practice is **least privilege**: RDS should accept **only database** traffic from **known clients** (Lambda + maybe your office). The SSM endpoint should accept **only** traffic from Lambda, not from random IPs on the internet.

**Do the three parts in order:** Part **A** first (B and C reference A).

---

### Part A — Lambda security group (do this first)

1. **EC2** → **Security Groups** → **Create security group**.
2. **Security group name:** `khanal-prod-sg-delivery-api-lambda`
3. **Description:** `Lambda delivery-api worker`
4. **VPC:** **`khanal-prod-vpc`**
5. **Inbound rules:** leave **empty** for now → **Create security group**.
6. Open that new group → **Inbound rules** → **Edit inbound rules** → **Add rule**:
   - **Type:** HTTPS  
   - **Port:** 443  
   - **Source:** **Custom** → paste **this same security group’s ID** (the `sg-xxxxxxxx` at the top of the page — a “self” rule for VPC endpoints).
7. **Save rules**.
8. **Copy the Security group ID** (`sg-…`) → you will paste it into **`serverless.yml`** under `vpc.securityGroupIds`.

---

### Part B — RDS security group

1. **Create security group** again.
2. **Name:** `khanal-prod-sg-rds-postgres`
3. **VPC:** **`khanal-prod-vpc`**
4. **Inbound** → **Add rule**:
   - **Type:** PostgreSQL  
   - **Port:** 5432  
   - **Source:** **Security group** → choose **`khanal-prod-sg-delivery-api-lambda`** (from Part A).
5. *(Optional — laptop access only)* **Add rule:** PostgreSQL **5432**, Source **My IP**.  
   - **Why:** Lets **`psql`** / DB tools from **your current home/office IP** reach RDS. Lambda is already allowed by rule 4.  
   - **Skip** if you only ever connect via Lambda or VPN/bastion (your IP changes on some networks).  
6. **Create security group** (saves **`khanal-prod-sg-rds-postgres`** in AWS).
7. **Later, in Step 8:** When you create the RDS database **`khanal-db-prod`**, under **Connectivity** → **VPC security groups**, select **`khanal-prod-sg-rds-postgres`** (do not leave only **`default`** unless you know what you’re doing).

---

### Part C — SSM VPC endpoint security group

1. **Create security group** again.
2. **Name:** `khanal-prod-sg-vpce-ssm`
3. **VPC:** **`khanal-prod-vpc`**
4. **Inbound** → **Add rule**:
   - **Type:** HTTPS  
   - **Port:** 443  
   - **Source:** **Security group** → **`khanal-prod-sg-delivery-api-lambda`**
5. **Create security group**.
6. In **Step 12** (SSM interface endpoint), choose **`khanal-prod-sg-vpce-ssm`** as that endpoint’s security group.

---

## Step 8 — RDS (PostgreSQL)

### 8a — DB subnet group (if not done)

1. **RDS** → **Subnet groups** → **Create**.
   - **Name:** `khanal-prod-db-subnet-group`
   - **VPC:** `khanal-prod-vpc`
   - **Subnets:** `khanal-prod-rds-1a` + `khanal-prod-rds-1b` (two AZs — **correct** for RDS).

### 8b — Create database — **cost-optimized** (good for Delhivery + Lambda)

Your console showed **~$1081/month** because of **`db.m7g.large`** + **`io2`** **400 GiB** + high **IOPS**. For this integration (webhooks, Postgres, moderate load), use **burstable `t4g` + `gp3`** instead.

Use **Create database** → **Standard create** → **Full configuration** (so you see every field). Set:

| Section | Field | **Use this (cheaper)** |
|---------|--------|-------------------------|
| Engine options | Engine type | **PostgreSQL** |
| | Engine version | **PostgreSQL 16.x** or **17.x** (LTS, widely supported). Avoid **18** unless you need it. |
| | Templates | **Dev/Test** *or* **Production** — then **override** instance + storage below to match this table. |
| **Availability and durability** | Deployment option | **Single-AZ DB instance deployment (1 instance)** — cheapest. Add **Multi-AZ instance** later only if you need automatic failover. **Do not** choose **Multi-AZ DB cluster (3 instances)** unless you really need that product (costly). |
| **Settings** | DB instance identifier | `khanal-db-prod` |
| | Master username | Your choice (e.g. `khanalfoods`) — lowercase is stored. |
| | Credentials | **Self managed** or **Secrets Manager** — your policy. |
| **Instance configuration** | DB instance class | **`db.t4g.micro`** (cheapest; ~1 GiB RAM) **or** **`db.t4g.small`** (~2 GiB) if you see memory pressure. **Burstable classes (t)** filter. |
| **Storage** | Storage type | **General Purpose SSD (`gp3`)** — **not** `io2` / **not** `Provisioned IOPS` for normal workloads. |
| | Allocated storage | **`20`**–**50` GiB** to start (increase later). |
| | Provisioned IOPS | **Leave default** for `gp3` (included baseline — **do not** buy extra IOPS unless metrics show you need it). |
| | Storage autoscaling | Optional **On**, **Maximum** e.g. **200** GiB (not 1000 unless you need it). |
| **Connectivity** | Virtual private cloud | **`khanal-prod-vpc`** |
| | DB subnet group | **`khanal-prod-db-subnet-group`** |
| | Public access | **Yes** — only if RDS subnets use **`khanal-prod-rt-public`** (Step 4) **and** you want `psql` from internet + SG rules. Requires **Step 1** DNS settings **on**. **No** if DB is private-only. |
| | **Existing VPC security groups** | Select **`khanal-prod-sg-rds-postgres`** only. **Remove `default`** — otherwise the wide **default** group can undo your careful rules. |
| | RDS Proxy | **Skip** (add later if you need pooling). |
| **Database authentication** | | **Password authentication** is enough unless you use IAM DB auth. |
| **Additional configuration** (expand) | Initial database name | e.g. **`khanal_serverless`** (so `DATABASE_URL` can use this DB). |
| | Backup retention | **`1`** day (cheap) or **`7`** days (more recovery comfort) — higher retention costs more storage. |
| | **Delete protection** | **Enable** for real prod (prevents accidental delete). |
| **Monitoring** | Performance Insights | **Off** for lowest cost — or **7-day** retention only (long paid retention adds cost). |
| | Database Insights | **None** unless you need paid fleet dashboards. |
| | Enhanced Monitoring | **Off** unless debugging CPU. |
| | DevOps Guru | **Off** (per-resource hourly charge if enabled). |

Then **Create database** and wait until **Status = Available**. Copy **Endpoint** → build:

`postgresql://MASTER_USER:PASSWORD@endpoint:5432/khanal_serverless`

### 8c — If estimate is still high — cut **allocated storage** first

Rough split on a **~$40** estimate with **`db.t4g.micro`**: most $ is often **disk**, not CPU.

| Your setting | Cheaper adjustment |
|--------------|-------------------|
| **Allocated storage 200 GiB** | Use **20–50 GiB** to start. Webhook + `delhivery_*` data is small; **Modify** RDS later to grow disk when **Free storage space** CloudWatch alarm says so. |
| **Autoscaling max 1000 GiB** | Cap at **100–200 GiB**, or turn autoscaling **Off** until you need it. |
| **`gp2` 200 GiB** | Switch to **`gp3`** with **20–50 GiB** where the console allows (same doc intent as Step 8b). |
| **Performance Insights** + long retention | **Disable** PI or keep only **free / short** retention. |
| **Backup retention** | **1** day in **Additional configuration** (less backup storage than **7**). |

**`db.t4g.micro` + Single-AZ** is already minimal for the instance — only move to **`db.t4g.small`** if you hit CPU credit or memory limits.

### 8d — When to spend more

| Symptom | Change |
|---------|--------|
| CPU credits / throttling | **`db.t4g.small`** or **`db.m7g.large`** |
| Disk latency / high IOPS need | Tune **`gp3`** IOPS or move to **Provisioned IOPS** only after metrics justify it |
| Must survive AZ failure | **Multi-AZ DB instance** (2 instances, higher cost) |

### 8e — After RDS is available

Copy the **endpoint** hostname and build **`DATABASE_URL`** for Lambda deploy.

---

## Step 9 — Database tables

On any machine that can reach RDS:

```bash
psql "$DATABASE_URL" -f sql/delhivery_lr.sql
```

---

## Step 10 — S3 bucket for EPOD

1. **S3** → **Create bucket**.
2. **Name:** `khanal-prod-delhivery-epod` — if taken, use `khanal-prod-delhivery-epod-YOURAWSACCOUNTID`
3. **Region:** **Asia Pacific (Mumbai) `ap-south-1`**
4. **Block all public access:** On
5. Create

Use this exact name for **`POD_S3_BUCKET`** when you deploy.

---

## Step 11 — VPC endpoint S3 (Gateway)

This adds a **Gateway** endpoint so traffic from subnets that use **`khanal-prod-rt-lambda-nat`** reaches **S3 inside the AWS network** (no hairpin over the public internet for that path). **Do not** attach this endpoint to **`khanal-prod-rt-public`** — only the **lambda-nat** route table.

1. **VPC** → **Endpoints** → **Create endpoint**.

2. **Endpoint settings**
   - **Name tag (optional):** `khanal-prod-vpce-s3` — creates a `Name` tag so you can find the endpoint later.
   - **Type:** **AWS services** (default). Do not pick Marketplace, EC2 Instance Connect, Resource endpoint, etc.

3. **Services**
   - **Service region:** **Asia Pacific (Mumbai) `ap-south-1`** (leave **Cross Region** off unless you have a special reason).
   - Search or filter for **`s3`**. The console shows **two** rows for the same service name **`com.amazonaws.ap-south-1.s3`**:
     - **Type = Gateway** ← **choose this row** (click it).
     - **Type = Interface** ← **do not** use for this guide; Gateway is the usual pattern for S3 from a VPC and does not need subnets or an endpoint security group.

4. **Network settings**
   - **VPC:** **`khanal-prod-vpc`** (your prod VPC).

5. **Route tables**
   - Expand the list and select **only** **`khanal-prod-rt-lambda-nat`** (the route table used by **`khanal-prod-lambda-nat-1a`** / **`khanal-prod-lambda-nat-1b`**).
   - **Leave unchecked:** the **Main** route table, **`khanal-prod-rt-public`**, and any unnamed / extra route tables — otherwise S3 traffic from those subnets would also use the endpoint, which you do not need for this design and can confuse troubleshooting.

6. **Policy**
   - **Full access** is fine to start: any principal in the VPC that already has IAM permission to your buckets can use the endpoint. IAM and bucket policies still apply.
   - **Custom** is optional later if you want the endpoint policy to deny everything except your EPOD bucket name.

7. **Create endpoint**.

**Note:** AWS may warn that existing connections from “affected” subnets that used **public** IPs to S3 could be **dropped** while the route updates. Avoid creating this during a critical deploy; for a new VPC it is usually harmless.

**After creation — route table:** Open the **lambda-nat** route table → **Routes**. You should see a new row whose **destination** is a **prefix list** (ID like **`pl-xxxxxxxx`**), not a CIDR. The **target** is your **S3 Gateway** endpoint (`vpce-…`). AWS adds this route **automatically** when you associate the endpoint with that route table; you do **not** create `pl-…` yourself. The exact **`pl-`** ID differs by region and can differ between accounts; it is **required** for private-subnet traffic to **S3** to use the endpoint instead of only the NAT path. Keep **`0.0.0.0/0` → nat-…`** and **`local`** as before.

---

## Step 12 — VPC endpoint SSM (Interface)

1. **Endpoints** → **Create**.
2. **Name:** `khanal-prod-vpce-ssm`
3. **Service:** `com.amazonaws.ap-south-1.ssm` → **Interface**
4. **VPC:** `khanal-prod-vpc`
5. **Subnets:** `khanal-prod-lambda-nat-1a` and `khanal-prod-lambda-nat-1b`
6. **Security group:** `khanal-prod-sg-vpce-ssm`
7. Create

---

## Step 13 — SSM parameters for SAP

**Skip this step** if you are **not** using SAP Service Layer from the Delhivery worker. You need it only when you deploy with **`SAP_SL_USE_SSM=1`** (or **`true`** / **`yes`**) so the worker reads config from Parameter Store instead of **`SAP_SL_*`** env vars. Behaviour and IAM are documented in [DELHIVERY.md](./DELHIVERY.md).

**Region:** create parameters in the **same region** as the stack (e.g. **`ap-south-1`**).

**Console path:** **Systems Manager** → **Parameter Store** → **Create parameter** (three times).

**Recommended settings (each parameter):**

| Field | Value |
|-------|--------|
| **Name** | Exact path below (`/sap/...` — leading slash, no typo). |
| **Tier** | **Standard** |
| **Type** | **`SecureString`** for all three (simplest; Lambda uses **`WithDecryption`**). **`String`** is also OK for **`/sap/sl_config`** only — it has no password. |
| **KMS key** | **`alias/aws/ssm`** (default) unless you use a custom CMK. |
| **Value** | A **single JSON object** (one line or pretty-printed — both are valid). |

### `/sap/sl_config`

Service Layer **base URL** (must include **`/b1s/v1`**) and **SAP company database** name.

**Example** (replace placeholders; **no trailing slash** on the URL is conventional):

```json
{
  "sap_b1_url": "https://sap.example.com:50000/b1s/v1",
  "companydb": "SBO_DEMO_US"
}
```

**Aliases** the code also accepts (same JSON, different keys): **`SAP_SL_BASE_URL`** instead of **`sap_b1_url`**, **`CompanyDB`** instead of **`companydb`**.

### `/sap/credentials`

Service Layer **login** user and password (same values you would put in **`SAP_SL_USERNAME`** / **`SAP_SL_PASSWORD`** when not using SSM).

**Example:**

```json
{
  "username": "manager",
  "password": "REPLACE_WITH_STRONG_PASSWORD"
}
```

**Aliases:** **`UserName`** / **`Password`** (SAP Login JSON style) work the same as **`username`** / **`password`**.

### `/sap/b1_session`

**Session cache** shared across Lambdas. The worker **reads** and **overwrites** this parameter when it logs in or refreshes cookies. You only need a **starting** value so the first run can acquire a lock and call **Login**.

**Initial value** (expired session — Lambda will replace on first SAP call):

```json
{
  "b1session": "",
  "routeid": "",
  "expires_on": "2000-01-01T00:00:00Z",
  "is_locked": false
}
```

| Key | Meaning |
|-----|--------|
| **`b1session`** | **`B1SESSION`** cookie / **`SessionId`** from SAP Login — leave empty at bootstrap. |
| **`routeid`** | **`ROUTEID`** cookie when present — may be empty. |
| **`expires_on`** | UTC time after which the worker treats the session as stale (ISO-8601 with **`Z`**, or `YYYY-MM-DD HH:MM:SS`). Use a **past** date until Lambda has logged in once. |
| **`is_locked`** | Concurrency lock; keep **`false`** for the seed. The worker sets **`true`** briefly while renewing. |

After a successful login, Lambda writes a fresh JSON (non-empty **`b1session`**, new **`expires_on`**, **`is_locked`: false). Do not hand-edit **`/sap/b1_session`** in production except to recover from a stuck lock (**`is_locked`: true** left after a crash — set back to **`false`** with empty session if needed).

### After parameters exist

1. Deploy **`delivery-api`** with **`SAP_SL_USE_SSM=1`** (see Step 15).  
2. Ensure the **SSM VPC interface endpoint** (Step 12) and **`SAP_SL_VERIFY_SSL`** are set appropriately (see [DELHIVERY.md](./DELHIVERY.md) if Service Layer uses a self-signed TLS cert).

---

## Step 14 — `serverless.yml` (delivery-api)

Edit **`services/delivery-api/serverless.yml`**:

- **`vpc.securityGroupIds`:** ID of **`khanal-prod-sg-delivery-api-lambda`**
- **`vpc.subnetIds`:** IDs of **`khanal-prod-lambda-nat-1a`** and **`khanal-prod-lambda-nat-1b`** only

---

## Step 15 — Deploy from your computer

The service template defaults region in YAML; for **Mumbai** pass region on deploy (or set `provider.region` in `serverless.yml` to `ap-south-1`).

```bash
bash scripts/build_shared_layer.sh
cd services/delivery-api
npm install
npx serverless deploy --stage prod --region ap-south-1
```

Set env vars for that shell (or use a `.env` you do not commit): **`DATABASE_URL`**, **`DELHIVERY_WEBHOOK_SECRET`**, **`POD_S3_BUCKET`**, **`SAP_SL_USE_SSM=1`**, **`SAP_SL_VERIFY_SSL`**, etc. See [DELHIVERY.md](./DELHIVERY.md).

---

## Step 16 — After deploy

1. **CloudFormation** → stack **`delivery-api-prod`** → output **`DelhiveryWebhookRestApiUrl`**
2. In **Delhivery**, set webhook URLs to that base + `/v1/webhooks/delhivery/...`
3. **CloudWatch Logs** → Lambda **`delhiveryWebhookWorker`** → check for errors

---

## Step 17 — GitHub Actions (optional)

Only if you want **manual deploys from GitHub** (this repo’s **Deploy** workflow: `workflow_dispatch`). No long‑lived **AWS access keys** in GitHub — use **OIDC** so Actions assumes an **IAM role** short‑lived.

### A — OIDC identity provider (once per AWS account)

1. AWS console → **IAM** → **Identity providers** (left nav).
2. **Add provider** → **OpenID Connect**.
3. **Provider URL:** `https://token.actions.githubusercontent.com`  
4. **Audience:** `sts.amazonaws.com`  
5. **Get thumbprint** (console fetches it) → **Add provider**.

If **`token.actions.githubusercontent.com`** already exists, skip to **B**.

### B — IAM role GitHub may assume (Create role wizard)

Do **Step A** first so **`token.actions.githubusercontent.com`** appears as an OIDC provider.

**Step 1 — Select trusted entity**

1. **IAM** → **Roles** → **Create role**.
2. Under **Trusted entity type**, choose **Web identity** — **not** “AWS service”, **not** “SAML 2.0 federation”, **not** “Custom trust policy” on this first screen (you can refine trust in JSON after creation).
3. **Identity provider:** choose **`token.actions.githubusercontent.com`** from the list.
4. **Audience:** **`sts.amazonaws.com`** (same value GitHub Actions sends).
5. If the console shows **GitHub organization** / **GitHub repository** (or similar), enter your **`OWNER`** / **`REPO`** (GitHub URL `github.com/OWNER/REPO`). If that UI is confusing or missing, continue anyway — you will tighten the trust policy in JSON in a later sub-step.
6. Click **Next**.

**Step 2 — Add permissions**

1. Attach policies so **`serverless deploy`** can manage the stack (CloudFormation, Lambda, IAM, API Gateway, SQS, Logs, layers, etc.).
2. **Simplest:** tick **`AdministratorAccess`** (only if you accept broad CI power and will lock **Trust** to one repo in the next section).
3. **Stricter:** start with **`PowerUserAccess`** and add IAM permissions until deploy succeeds (often several `iam:*` actions for roles Serverless creates).
4. Click **Next**.

**Step 3 — Name, review, and create**

1. **Role name:** e.g. **`khanal-prod-github-deploy`**.
2. **Create role**.

**After creation — lock trust to your repo (required)**

1. Open the new role → **Trust relationships** → **Edit trust policy**.
2. Replace the policy with the JSON below (substitute **`AWS_ACCOUNT_ID`**, **`OWNER`**, **`REPO`**). This prevents other GitHub repos from assuming the role.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::699013266218:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:Harshatest-lab/Khanal_tech_serverless:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

To allow **only the `main` branch** (stricter), use:

`"token.actions.githubusercontent.com:sub": "repo:OWNER/REPO:ref:refs/heads/main"`

3. **Update policy**.

Copy the role **ARN** (e.g. `arn:aws:iam::123456789012:role/khanal-prod-github-deploy`) → GitHub secret **`AWS_ROLE_ARN`**.

**Alternative (advanced):** If you prefer a single JSON trust from the start, choose **Custom trust policy** on **Step 1** instead of **Web identity**, paste the JSON above (with placeholders filled), then continue to **Step 2** permissions and **Step 3** naming.

### C — Permissions on that role

**Serverless** creates/updates **CloudFormation**, **Lambda**, **IAM roles**, **API Gateway**, **SQS**, **CloudWatch Logs**, **Lambda layers**, etc. Easiest path for a small team:

- Attach **`AdministratorAccess`** to this role **only if** you accept broad CI power and you locked the trust policy to **one repo** (and ideally **one branch**).

Tighter option (more work): start from **`PowerUserAccess`** and add whatever deploy fails on (often **`iam:*`** for roles Serverless creates). Iterate until `serverless deploy` succeeds, then trim.

### D — GitHub repository secrets

**GitHub** → your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Required for the workflow (see **`.github/workflows/deploy.yml`**):

| Secret | Example / notes |
|--------|------------------|
| **`AWS_ROLE_ARN`** | `arn:aws:iam::ACCOUNT_ID:role/khanal-prod-github-deploy` |
| **`AWS_REGION`** | `ap-south-1` (must match where you deploy the stack) |
| **`DATABASE_URL`** | Same URI as Step 15 (`…?sslmode=require`, correct DB name). |
| **`DELHIVERY_WEBHOOK_SECRET`** | Same as prod Delhivery header secret (or empty if not used). |
| **`POD_S3_BUCKET`** | e.g. `khanal-prod-delhivery-epod` |
| **`POD_S3_PREFIX`** | Optional; e.g. `delhivery-pod` or leave unset if workflow omits empty — if deploy fails, add with a value. |

Optional (SAP / same semantics as Step 15):

| Secret | Notes |
|--------|--------|
| **`SAP_SL_USE_SSM`** | e.g. `1` if using Parameter Store. |
| **`SAP_SL_BASE_URL`** / **`SAP_SL_COMPANYDB`** / **`SAP_SL_USERNAME`** / **`SAP_SL_PASSWORD`** | Only if **not** using SSM for SAP. |
| **`SAP_SL_VERIFY_SSL`** | `true` or `false` |

The workflow runs: `npx serverless deploy --stage <input> --region <AWS_REGION secret>`. Pick **stage** `prod` and **service** `delivery-api` when you run the workflow.

### E — Run a deploy from GitHub

1. **Actions** → **Deploy** → **Run workflow**.
2. **Stage:** `prod` (or `staging` / `dev` as you use).
3. **Service:** `delivery-api`.
4. Confirm the job **Configure AWS credentials (OIDC)** succeeds, then **Install & deploy**.

If OIDC fails with **“Not authorized to perform sts:AssumeRoleWithWebIdentity”**, fix the role **trust policy** (`sub` / `aud`) and the **OIDC provider** ARN.

---

## Name cheat sheet (production)

| What | Name you type in AWS |
|------|----------------------|
| VPC | `khanal-prod-vpc` |
| Internet Gateway | `khanal-prod-igw` |
| Subnet (public a) | `khanal-prod-public-1a` |
| Subnet (public b) | `khanal-prod-public-1b` |
| Subnet (Lambda a) | `khanal-prod-lambda-nat-1a` |
| Subnet (Lambda b) | `khanal-prod-lambda-nat-1b` |
| Subnet (RDS a) | `khanal-prod-rds-1a` |
| Subnet (RDS b) | `khanal-prod-rds-1b` |
| Route table (internet) | `khanal-prod-rt-public` |
| Route table (Lambda NAT) | `khanal-prod-rt-lambda-nat` |
| Elastic IP | `khanal-prod-nat-eip` |
| NAT Gateway | `khanal-prod-nat-gw` |
| SG Lambda | `khanal-prod-sg-delivery-api-lambda` |
| SG RDS | `khanal-prod-sg-rds-postgres` |
| SG SSM endpoint | `khanal-prod-sg-vpce-ssm` |
| DB subnet group | `khanal-prod-db-subnet-group` |
| RDS instance | `khanal-db-prod` |
| S3 bucket | `khanal-prod-delhivery-epod` (or + account id) |
| Endpoint S3 | `khanal-prod-vpce-s3` |
| Endpoint SSM | `khanal-prod-vpce-ssm` |
| GitHub deploy role | `khanal-prod-github-deploy` |

---

## More detail

Webhook env vars, SAP flow, TLS: [DELHIVERY.md](./DELHIVERY.md)  
Lambda + RDS pattern: [LAMBDA_VPC_SETUP.md](./LAMBDA_VPC_SETUP.md)
