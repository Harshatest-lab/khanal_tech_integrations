# Lambda (delivery-api) in VPC with RDS / RDS Proxy — dev and prod

Use the **same pattern in dev and prod**; only **subnet IDs, security group IDs, and `DATABASE_URL`** change per environment.

## Why

- **Private path** from Lambda to **RDS Proxy** or **RDS** (shorter RTT than public internet, stable SG rules).
- Aligns with [DATABASE.md](DATABASE.md) (RDS Proxy, `sg-lambda` → `sg-proxy` → `sg-rds`).

## Prerequisites (both stages)

1. **VPC** with at least **two private subnets** in different AZs (Lambda requirement).
2. **RDS** (or cluster) in **private subnets** — **not** publicly reachable for production; dev can mirror prod topology.
3. **RDS Proxy** (recommended) in the **same VPC**, target = your RDS; note the **proxy endpoint** (and port).
4. **Security groups**
   - **`sg-lambda`** (attach to Lambda): egress **5432** → `sg-proxy` (or `sg-rds` if no proxy); egress **443** if Lambda still calls the internet (use **NAT gateway** in private subnets, or **VPC endpoints** for AWS APIs to avoid NAT where possible).
   - **`sg-proxy`**: ingress **5432** from `sg-lambda`; egress **5432** to `sg-rds`.
   - **`sg-rds`**: ingress **5432** only from `sg-proxy` (or from `sg-lambda` if you skip proxy in dev — not ideal).
5. **IAM**: attaching a VPC to Lambda requires extra EC2 permissions. **Serverless Framework** normally adds **`AWSLambdaVPCAccessExecutionRole`** when `provider.vpc` is present — verify the generated role after first deploy.
6. **`DATABASE_URL`** at deploy time must point at the **proxy** (or RDS) hostname **reachable from inside the VPC** (not a public-only endpoint unless you intentionally route that way).

## Wire `delivery-api` (Serverless)

1. Open [`services/delivery-api/serverless.yml`](../services/delivery-api/serverless.yml).
2. Under **`delhiveryWebhookWorker`**, set **`vpc.securityGroupIds`** and **`vpc.subnetIds`** (the HTTP **ingest** function has **no** VPC; only the SQS worker talks to RDS).
3. Replace **`subnet-*`** and **`sg-*`** with your **dev** subnets and **`sg-lambda`** for dev deploys; use **prod** values for prod deploys (different `serverless deploy --stage prod` or separate CI variables).
4. Deploy from `services/delivery-api` (or your CI) with **`DATABASE_URL`** set to the **dev** DSN first, then **prod**.

**Tip:** Keep real IDs out of git if policy requires it — use **CI secrets** or **SSM Parameter Store** and paste into deploy env, or generate `serverless.yml` from a private template in CI.

## Validate

- After deploy, invoke the webhook; check **CloudWatch** `REPORT` for **`Init Duration`** and **`Duration`** vs previous public-DB path.
- Confirm **no** security group or subnet mismatch (Lambda timeouts / `Task timed out` often = SG or route table).

## Cold starts and Delhivery latency

VPC improves **DB RTT**; it does **not** remove Lambda **cold init**. Some invocations can still exceed a strict **≤ 500 ms** wall-clock budget when a **new** execution environment starts—monitor **`Init Duration` + `Duration`** in CloudWatch Logs (`REPORT`) and **p99** over real traffic.

## References

- [DATABASE.md](DATABASE.md) — RDS Proxy and SG layout.
- [AWS: Configuring Lambda VPC access](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
