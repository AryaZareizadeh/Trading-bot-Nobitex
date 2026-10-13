# Portfolio Manager usability review

Reviewed and updated on 2026-10-01.

## Problems addressed

| Problem | Change |
| --- | --- |
| Paper trading and exchange history competed for attention. | Paper setup, validation and performance form one numbered flow. Exchange history is expandable, with its own snapshot action. |
| Failed exchange requests prevented the public-data paper manager from loading. | Both sections load independently, with separate failure messages. |
| Starting, pausing and researching gave little feedback or allowed duplicate submissions. | Actions show progress and success/failure messages; controls reflect running and pending states. API validation and non-JSON errors are handled. |
| Refresh overwrote unchecked assets and editable budgets contradicted the saved portfolio. | Unsaved selections survive refresh. Saved assets and budget are locked; resume uses the saved setup. |
| A running service without a usable model appeared ready. Old errors persisted after recovery. | Readiness comes from the actual model artifact, with asset coverage and actionable explanations. Failed update reasons are reported and successful updates clear them. |
| Dense research prose hid model comparisons and acceptance failures. | A comparison table, validation checklist and data coverage table expose those results. Previous reports are identified while a newer run is pending or failed. |
| Incomplete valuation could display the starting balance and zero drawdown. | Missing values remain unavailable. Observation/valuation timestamps expose freshness, and charts omit invalid/incomplete points. |
| A slow refresh could undo the displayed result of a successful paper action. | Each action forces a fresh read; superseded responses cannot overwrite the latest state. Polling stops when leaving the tab. |
| Fully sold assets could crash valuation if their market price disappeared. | Zero quantities do not require market or liquidation prices. |
| Paper setup could not be reset, interrupted research stayed running, and stalled requests left controls disabled. | Reset backs up and clears the current paper portfolio. Interrupted research becomes retryable. Requests time out after 20 seconds and release the controls. |

The layout keeps the dashboard's existing palette and typography, uses native
expandable sections, labeled inputs, accessible status announcements and
scrollable tables, and stacks panels on smaller screens.

## Remaining limitations

- A saved paper portfolio has a fixed budget and asset list until **Reset paper
  portfolio** is used. Reset saves a backup and unlocks fresh setup; there is no
  interface for browsing/restoring those backups or managing multiple portfolios.
- Paper holdings expose quantities and aggregate equity, but no current
  per-asset market value or allocation weight. Old fill prices should not be
  presented as current valuations.
- Research exposes running/completed/failed states, but no percentage progress,
  time estimate or cancellation endpoint.
- Prior research runs and complete reports are available as JSON; there is no
  interactive experiment comparison or export interface.
- Paper deposits and withdrawals are not exposed. Exchange cash flows must be
  entered manually; those flows affect only exchange performance.
- A runnable model can fail the stated validation criteria. Model readiness does
  not mean those criteria passed or that current public price feeds are fresh.
- Estimates remain paper estimates. Historical candle-based fills cannot fully
  model order queues, spread changes, market impact or actual execution.

## Verification

UI regression coverage is in `tests/portfolio_manager_ui.test.cjs`, alongside
the existing `tests/dashboard_ui.test.cjs`. Backend regression coverage is in
`tests/test_spot_manager_status.py`, alongside existing portfolio and spot tests.

Browser checks used a disposable localhost preview with synthetic paper
balances and a saved example research report. They covered first use, preserved
setup edits, inline validation, research and pause/resume, and desktop/mobile
layout without page overflow. No exchange credentials or real orders were used.

Commands:

```powershell
node --test --test-isolation=none tests/dashboard_ui.test.cjs tests/portfolio_manager_ui.test.cjs
python -m pytest tests/test_spot_manager.py tests/test_spot_manager_status.py tests/test_portfolio.py tests/test_dashboard_flow.py tests/test_auth.py tests/test_multiuser.py tests/test_imports.py -p no:cacheprovider --basetemp .runtime/pytest-portfolio-manager -q
```
