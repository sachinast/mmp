# 10 — QA & Testing Strategy

**Status:** describes the built system. Counts from `make check` on 2026-09-09:
**784 Python tests, 87 SDK tests**.

## The core practice: mutation testing

Passing tests prove a suite runs. They do not prove it would notice if the code
were wrong. So every control gets deliberately broken and the suite must react.

This found more real problems than any other practice used here:

- A `SET LOCAL` tenancy test that passed for the wrong reason.
- A cache-tenant test protected by an earlier check, not its own assertion.
- An identity-query guard that accepted a JOIN condition as a filter.
- A provider-name leak regex with two boundary bugs.
- `record()` ignoring the postback adapter's verdict entirely — `interpret()`
  was decorative, so a `200` carrying `{"error": …}` counted as delivered.
- A fraud sweep query that inflated install counts through a join fanout, which
  would have manufactured a flooding finding out of arithmetic.

### Two traps in the method itself

**Self-referential boundary tests.** A test that derives its boundary from the
constant it is testing follows that constant when it moves. Thresholds are now
pinned as literals in a dedicated test, so changing one breaks a named test.

**Stale bytecode.** `sed` replacing a two-digit constant leaves the file size
unchanged, and within the same second the mtime matches too — so Python served a
cached `.pyc` and mutants appeared to survive that had never been loaded. The
harness now runs with bytecode disabled, restores from a git-clean baseline, and
asserts the mutation actually reached the interpreter.

## Layers

| Layer | What it covers |
|---|---|
| Unit | Pure functions: attribution, fraud rules, templates, deep link validation, SKAdNetwork |
| Integration | Real PostgreSQL and Redis, real RLS, real migrations |
| End-to-end | Click → install → attribution → postback through the actual services |
| Contract | The TypeScript SDK against the Python server |
| Property | Round-trip invariants (API key encoding) |
| Conformance | SKAdNetwork against Apple's own published signatures |
| Static | Source assertions on native code that cannot be executed here |
| Performance | Latency gate in CI |
| Security | bandit, pip-audit, an f-string SQL ban with its own test |

### Conformance testing deserves emphasis

`tests/test_skadnetwork.py` verifies signature handling against **five postbacks
Apple published together with the signatures their private key produced**. A
suite that signs its own fixtures with its own key proves the ECDSA call works
and proves nothing about whether the canonical string matches Apple's — which is
the only part that can realistically be wrong. A subtly wrong field order
rejects every genuine postback, and the tempting fix for "nothing verifies" is
to stop verifying.

### Cross-language contract testing

`tests/test_sdk_contract.py` is Python and parses the TypeScript SDK source,
asserting the batch limits, reserved event names, consent vocabulary, wire
fields and endpoint paths all match what the server enforces. TypeScript and
Python otherwise agree only by convention, and the symptom of drift is a `422`
in an already-shipped app. Seven deliberate drifts were introduced; all seven
were caught.

## Gates

```
make check    # lint, mypy --strict, bandit, pytest, SDK tests, Swift typecheck
make bench    # latency; fails if medians regress past 160% of baseline
make audit    # pip-audit --strict against the lockfile
```

Migrations are verified against an **empty** database, not only as an increment
— a migration that reads live ORM metadata broke the chain from empty once, and
a slow test now applies the whole chain.

## What testing has not covered

Stated plainly:

- **No device testing.** No postback from a real handset, no React Native app
  built end to end. The iOS core is executed on a simulator; the Kotlin is not
  compiled anywhere in CI.
- **No load testing from separate hardware.** All latency figures are loopback
  on one machine and are not an SLO.
- **No chaos or failure-injection testing** beyond unit-level simulation.
- **No external security testing.**
- **Fraud thresholds are reasoned, not measured** against labelled data.

## Practices worth keeping

- **Look at the rendered output.** Three dashboard bugs were found by opening
  the page in a browser and none by an assertion.
- **Test the skip path, not just the happy path.** Two Makefile targets had a
  broken skip because make gives each recipe line its own shell — found only by
  running them on a machine pretending to lack the toolchain.
- **Assert that a patch applied.** Silent `str.replace` no-ops wasted time at
  least four times.
- **Scope assertions to the fixture.** Three tests passed alone and failed in
  the suite because they counted rows globally.
