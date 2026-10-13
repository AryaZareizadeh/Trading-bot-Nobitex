# Project Review Remediation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement and review the independent workstreams. Checkboxes track implementation and verification.

**Goal:** Repair all twelve review findings and implement the six operational, validation, CI, coordination, observability, and maintainability improvements.

**Architecture:** Preserve the single-process FastAPI dashboard, SQLite authority, per-user stores, stopped-on-restart behavior, and existing API contracts. Introduce small shared lifecycle, scheduling, settlement, backup, and monitoring units where they remove duplicated safety logic. Use separate offline regressions for temporal correctness, recovery, session isolation, and resource contention.

**Tech Stack:** Python 3.13, FastAPI, SQLite, pandas, SciPy/Optuna, optional PyTorch, vanilla JavaScript and Node's test runner, Docker Compose.

**Spec:** `output/project-review-2026-10-02.md` (twelve findings and six additional improvements).

## Global constraints

- Preserve all existing uncommitted work; do not reset files or commit unrelated changes.
- Implement in the current `ShortLongSeperation` checkout because reviewed additions exist only in its working tree; creating a default-branch worktree would omit them.
- Never read operator secrets, place live orders, start production bots, overwrite runtime data, deploy, publish, or send external messages.
- Keep migrations compatible and restore bots stopped. Persist order resolution and runtime together.
- Reject uncertainty conservatively; `Expired`/`Liquidated` can still await system settlement.
- Keep GPU training serialized, but do not occupy live HTTP executor workers while queued.
- Tests use isolated storage and synthetic providers. Root owns full-suite integration verification; agents run focused regressions without duplicating full suites concurrently.
- The user explicitly authorized planning and execution together. Continue through phases without another plan approval gate.

## Review focus

- Actual fills occurring after older exit signals must retain the real entry price and never close before entry.
- Unknown close response followed by partial fill, cancellation, or expiration must reconcile without duplicate submission.
- Switching accounts during any pending financial request must hide old data and ignore late success/failure responses.
- Missing bars, unavailable BTC context, provider calendar alignment, and price gaps must not fabricate features or returns.
- Restoring into a fresh directory must preserve registry/artifact consistency and leave live bots stopped.

## Task 1: Live positions, close recovery, expiry and calendar timing

**Ownership:** `strategy.py`, `bot.py`, `exchange_execution.py`, new settlement/interval helpers if needed, execution tests.

- [ ] Add regressions reproducing historical seeded exits, duplicate organic entries, long/short close binding, settled and unsettled expiry, and 28/29/30/31-day transitions; observe failures.
- [ ] Evaluate seeded live exits only at/after actual entry, preserve actual owned fill state, and avoid duplicate entries from historical replay.
- [ ] Persist position direction and execution side distinctly; validate matching close orders and settlement consistently.
- [ ] Use calendar-aware candle boundaries for append, refresh, live boundary and wake timing.
- [ ] Offload delegation lookup; add durable, sanitized trading audit events and structured logging without exposing credentials.
- [ ] Run focused execution/recovery/runtime/strategy tests and review the diff.

Acceptance examples: `assert close_time >= opened_at`; `assert bind_valid_close(long, sell)`; `assert unsettled_expired_intent_remains_unresolved`; February-to-March appends successfully.

## Task 2: Recovery workload, rate limits and browser session lifecycle

**Ownership:** `accounts.py`, `auth.py`, `multiuser.py`, `static/index.html`, new `static/session.js`, security and frontend tests. Root owns `server.py` integration.

- [ ] Add failures for hashing unusable recovery codes, whitespace rate-limit bypass, stale financial responses and cash-flow DOM retention.
- [ ] Canonicalize email before lookup and account/IP limiting; reject unusable codes before hashing, bound async hashing work, preserve atomic consume and failed-attempt counts.
- [ ] Extract reusable session/request generation and cancellation logic; apply it across dashboard requests, clear private state on logout and reload the active view on login.
- [ ] Add a transactional credential identity ownership facility; expose a verified profile identity integration hook for root's credential endpoint, retaining safe migration of existing identities.
- [ ] Run auth/multi-user and all relevant Node regressions, including existing trainer/optimizer guards.

Acceptance examples: `assert hash_calls == 0` for no/expired/exhausted/wrong code; variants share one email bucket; account A's late response cannot render after account B login.

## Task 3: Research chronology, PriceAI continuity and stable BTC context

**Ownership:** `spot_research.py`, `spot_paper.py`, `spot_portfolio.py`, `price_ai.py`, research tests. Coordinate any required direct bot caller change with Task 1.

- [ ] Add failures for future-fill/old-mark gaps, missing interval labels/features, and BTC feed omission.
- [ ] Separate decision and future fill events, record actual fill times, use contemporaneous valuation, and finish each evaluation with a complete boundary mark.
- [ ] Validate or invalidate discontinuous PriceAI feature/label windows in direct and saved model paths.
- [ ] Fetch BTC context independently of the allocation universe; keep unavailable required features unavailable instead of substituting asset returns.
- [ ] Move growing paper observations/fills to append-only durable records while keeping bounded UI/history views and migrating existing embedded history without loss.
- [ ] Version affected research artifacts and explain rerunning old reports rather than rewriting them.
- [ ] Run spot/research/PriceAI integration checks and review the diff.

Acceptance examples: unchanged price after a gap creates no positive return; a four-hour label never spans five hours; missing BTC means abstention; history survives restart without repeatedly rewriting all observations.

## Task 4: Complete portfolio metrics and recoverable backup bundles

**Ownership:** `portfolio.py`, `storage.py`, `main.py`, new `backup.py`, portfolio/storage/backup tests.

- [ ] Add a >5,000-snapshot regression and a fresh-directory model artifact restore failure.
- [ ] Stream full-history performance, keep bounded chart output, and expose period boundaries.
- [ ] Retain database-only backup; add bundle backup/verify/restore commands with online SQLite backup, immutable artifacts, manifest hashes, safe member paths, explicit empty restore destination, and consistency checks.
- [ ] Include multi-user account/tenant stores and public candle cache; exclude secrets, lock files, and transient state. Keep the master key separately protected.
- [ ] Test preservation, corruption/path traversal rejection, incomplete artifacts, and restored models/bots remaining stopped.

Acceptance examples: `performance(5001).return_pct == performance(5000).return_pct`; verified bundle restores every ready model's checkpoint; malicious archive members never escape the destination.

## Task 5: Scheduling, account coordination and runtime diagnostics

**Ownership:** new `research_scheduler.py`, new `runtime_status.py`/router, `model_trainer.py`, `server.py`, scheduler/status/integration tests. Coordinate `multiuser.py` interfaces with Task 2.

- [ ] Test that queued research does not consume default live executor threads, cancellation releases slots, and accounts cannot monopolize research capacity.
- [ ] Add bounded async admission with dedicated worker execution; retain one neural trainer globally and configurable shared spot research capacity.
- [ ] Integrate verified exchange identity from the supplied credential's profile with transactional ownership; reject duplicate tenant ownership without disclosing the other account.
- [ ] Add authenticated operational status for data/exchange age, unresolved intents, bot errors, job/queue state and audit history. Add a small unauthenticated liveness endpoint with no private information for Docker.
- [ ] Extract the monitoring/trainer routes into focused router units as part of integration; preserve endpoint shapes and auth middleware behavior.
- [ ] Test queue responsiveness, identity conflict and restart behavior, and status authorization/redaction.

## Task 6: Validation-first optimizer promotion

**Ownership:** `optimizer.py`, relevant `server.py` schemas/calls, frontend optimizer changes after Task 2, optimizer tests.

- [ ] Add regressions for ordinary optimization's default split, frozen selection versus untouched test, and source-specific transfer evaluation.
- [ ] Default ordinary searches to chronological training with forward validation and final holdout; retain an explicitly labeled in-sample option for exploration and legacy readability.
- [ ] Evaluate chosen parameters on aligned Nobitex data before promotion; unavailable transfer data is displayed honestly and blocks real promotion, while simulation drafts remain explicit.
- [ ] Persist seed, source, cost assumptions, selection rule, training/validation/test windows and search budget; surface these in results.
- [ ] Run DE/TPE/independent optimizer and frontend integration checks.

## Task 7: CI, documentation, packaging and final review

**Ownership:** `.github/workflows/tests.yml`, `tests/conftest.py`, new browser/container smoke tests, dependency manifests when needed, `.gitignore`, `.dockerignore`, `Dockerfile`, README/architecture/operations/research docs.

- [ ] Run Node checks in Windows/Linux CI and add optional CPU-PyTorch numerical/serialization CI.
- [ ] Block urllib as well as requests in offline tests; make position-limit refresh honor offline mode.
- [ ] Add small real-browser login/account-switch/tab smoke coverage and Docker startup/bundle restore smoke coverage using temporary data.
- [ ] Exclude generated artifacts from Docker/Git defaults, document retention and current architecture, complete backup/restore and research validation behavior.
- [ ] Obtain independent spec/code review of every workstream and the integrated result; repair material findings.
- [ ] Run full Python and Node suites, dependency checks and available browser/container/Torch validation; report any environmental limitations precisely.
- [ ] Record completed tasks and evidence in this plan, then provide the final change summary and remaining deployment actions.

## Execution ledger

- Plan created; prior verification baseline: 616 Python passes, 31 optional Torch skips; 128 Node passes.
- Ruling: use the current dirty feature checkout and do not commit — the reviewed work is present here, and preserving the user's changes is required.
- Ruling: implement security fixes within the authorized local task; authorization to deploy or place orders is neither inferred nor needed for these code changes.
