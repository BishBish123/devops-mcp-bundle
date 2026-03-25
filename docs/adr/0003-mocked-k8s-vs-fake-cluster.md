# ADR-0003: mocked `kubernetes_asyncio` vs a fake cluster (kind/minikube)

- **Status:** accepted
- **Date:** 2026-01-08
- **Author:** Bishara Mekhaeil

## Context

The k8s server needs tests. The most realistic option is a real
cluster (e.g. `kind`, `minikube`, `k3d`); the cheapest is mocking
`kubernetes_asyncio.client.CoreV1Api`. The middle ground —
`fake-kubectl`-style server — exists but is more friction than either
extreme.

## Considered options

### A. Real `kind` cluster in CI

- **Pro:** highest fidelity. The bundle's `pod_logs` would be
  exercised against a real kubelet stream. Misuses of the API would
  fail loudly.
- **Con:** boots in 30–60s. Doubles CI time. Adds a Docker dependency
  to local testing. Most of the bundle's logic is response-shaping,
  not API-call-shaping — there's nothing the real cluster catches that
  a mock can't.

### B. Mocked `CoreV1Api` (chosen)

- **Pro:** tests run in milliseconds. Easy to construct adversarial
  shapes (missing fields, extra annotations, weird timestamps). Easy
  to assert on the parameters the bundle passes (e.g. that
  `pod_events` calls `list_namespaced_event(field_selector=…)`).
- **Con:** doesn't catch API-version drift. If `kubernetes_asyncio`
  changes a method signature, our mocks will keep passing while real
  callers break.

### C. `fake-k8s-server` HTTP shim

- **Pro:** sits between mock and real — the HTTP layer is exercised.
- **Con:** unmaintained; the bundle's API surface is small enough that
  the marginal value over (B) is tiny.

## Decision

**B.** Mocks via `unittest.mock`, asserting on call shape and
returning fixture-shaped objects (`Mock(metadata=…, status=…, spec=…)`).
We accept the API-drift risk and pin `kubernetes_asyncio>=32.0` in
pyproject; if a future upgrade breaks the bundle, the integration test
matrix (which we run on a real `kind` once per release) will catch it.

## Consequences

- `tests/test_k8s/*.py` runs in <1s on a laptop.
- New k8s features land in two places: a mock-driven unit test plus
  the actual queries.py change. The pattern in `_to_pod` is the
  template — shape the mock, call the helper, assert on the model.
- The README's "test" make target runs only the mocked tests; an
  optional `make test-k8s-real` will boot `kind` and run the full
  suite, but it's not on the default path.

## Follow-ups

- When integration tests are added (the directory exists,
  `tests/test_postgres/test_queries_integration.py` already does it
  for Postgres), the matching k8s suite should mirror the structure:
  marker `@pytest.mark.integration`, opt-in via `make test-integration`.
