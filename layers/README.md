# Shared Lambda layer

AWS expects Python layers to expose packages under `python/` at the zip root.

## Build

From repository root:

```bash
bash scripts/build_shared_layer.sh
```

This writes `layers/build/python/khanal_shared/...` which Serverless zips for the `shared` layer defined in each service.

## Why a layer

- One artifact reused by many Lambdas.
- Faster deploys when only function code changes.
- Smaller per-function zip (only `src/` + deps).

Third-party libraries used **only** by shared code should still be declared in each function's `requirements.txt` unless you vendor them into the layer (advanced).
