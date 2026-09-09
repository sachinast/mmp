# Documentation

## For developers integrating the platform

- **[API quickstart](api/QUICKSTART.md)** — start here
- **[API reference](api/README.md)** — every endpoint, generated from the service
- **[openapi.json](api/openapi.json)** — machine-readable; point a generator at it
- **[SDK README](../sdks/react-native/README.md)** — React Native

## Specification

Numbered documents in **[spec/](spec/00-index.md)**. Each carries a status
header; documents 08 (Billing) and 13 (AI) describe systems that **do not
exist**.

## Subject documents

Written alongside the code they describe.

- **[SECURITY.md](SECURITY.md)** — the authoritative security document
- **[FRAUD.md](FRAUD.md)** — fraud rules, thresholds, and known gaps
- **[SKADNETWORK.md](SKADNETWORK.md)** — Apple attribution
- **[DEVICE_TESTING.md](DEVICE_TESTING.md)** — what only real hardware can verify
- **[API_VERSIONING.md](API_VERSIONING.md)** — what counts as breaking
- **[PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md)** — what blocks going live

## Keeping these honest

The API reference is generated. To refresh it after changing routes:

```bash
uv run python infra/docs/generate_api_docs.py
```

Everything else is written by hand and will drift. When it does, the failure
mode is worse than having no document — a stale specification is trusted and
wrong. If you change behaviour these describe, change them in the same commit.
