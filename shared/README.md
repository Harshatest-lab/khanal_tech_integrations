# khanal-shared

Install editable from repo root:

```bash
pip install -e "./shared[dev]"
```

This package is bundled into Lambdas via `package.patterns` in each `serverless.yml`, or via a **Lambda Layer** (recommended at scale — see `layers/shared/README.md`).
