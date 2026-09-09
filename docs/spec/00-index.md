# Specification index

Every document carries a status in its header. Read it first — several of these
describe systems that **do not exist yet**, and they are written as proposals
rather than as descriptions of the platform.

| # | Document | Status |
|---|---|---|
| 01 | [Product Requirements](01-prd.md) | Describes built system |
| 02 | [MVP & Product Scope](02-scope.md) | Describes built system |
| 03 | [User Roles & RBAC](03-rbac.md) | Describes built system |
| 04 | [User Flows & UX](04-flows.md) | Mixed — dashboard is partial |
| 05 | [System Architecture](05-architecture.md) | Describes built system |
| 06 | [Database / ERD](06-database.md) | Describes built system |
| 07 | [API Specification](07-api.md) | Describes built system |
| 08 | [Billing & Subscription](08-billing.md) | **NOT BUILT** — proposal |
| 09 | [Security & Compliance](09-security.md) | Describes built system |
| 10 | [QA & Testing Strategy](10-testing.md) | Describes built system |
| 11 | [Analytics & Event Tracking](11-analytics.md) | Living |
| 12 | [DevOps / Deployment](12-devops.md) | Living — partly aspirational |
| 13 | [AI Specification](13-ai.md) | **NOT BUILT** — proposal |
| 14 | [Integrations](14-integrations.md) | Living |
| 15 | [Product Roadmap](15-roadmap.md) | Living |
| 16 | [Change Log](16-changelog.md) | Living |

Plus, for people integrating rather than building:

- [API reference](../api/README.md) — generated from the running service
- [Integration quickstart](../api/QUICKSTART.md)

## A note on status

"Describes built system" means the document was written from the code, and in
most cases from extracting the real schema, routes and roles rather than from
memory. Where a document states a number — endpoint counts, table counts, test
counts — it came from the running system on 2026-09-09.

"NOT BUILT" means exactly that. Documents 08 and 13 specify things nobody has
implemented, and 13 in particular describes a capability the platform
deliberately does not have today. They are decision documents, not records.
