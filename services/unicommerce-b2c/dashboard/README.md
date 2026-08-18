# Unicommerce B2C — Ops Console

React + TypeScript + Tailwind + shadcn-style UI. Talks directly to the
`unicommerce-b2c` HTTP API and to Cognito (no backend login proxy) — same
auth pattern as before, just rebuilt on a proper component stack.

## Develop

```bash
npm install
npm run dev      # http://localhost:8090
```

## Build & deploy

```bash
npm run build     # outputs to dist/
aws s3 sync dist/ s3://unicommerce-b2c-prod-dashboard-site --delete
aws cloudfront create-invalidation --distribution-id <DashboardDistribution id> --paths "/*"
```

Get the bucket name and distribution ID from the stack outputs
(`DashboardSiteBucketName`, `DashboardCloudFrontUrl`).
