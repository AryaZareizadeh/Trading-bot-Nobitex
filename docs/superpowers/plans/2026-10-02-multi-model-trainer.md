# Multi-model Trainer Implementation Plan

**Goal:** Put every supported learned-model pipeline in its own Model Trainer sub-tab and add a Transformer generating ten complete OHLCV candles from one hundred completed candles.

**Architecture:** A backend catalog describes each family's defaults, data requirements, availability and bot compatibility. The existing account-scoped background manager dispatches training, checkpoint persistence and previews by architecture. The frontend retains independent family drafts and views while keeping active-run cancellation accessible.

**Tech stack:** Existing FastAPI, NumPy/Pandas, optional PyTorch, scikit-learn/joblib and optional LightGBM; existing dashboard JavaScript.

**Authorization:** The user approved the proposed design with “do it”; implement directly in the existing checkout and preserve its unrelated changes.

## Constraints and review focus

- Transformer defaults and shape are fixed at 100 historical candles and 10 generated candles; predictions include open, high, low, close and volume.
- Chronological validation/test splits purge future labels; scalers only see training data. Transformer validation uses recursive predictions, not teacher-forced inputs.
- All output candles must have positive prices, nonnegative volume and valid high/low bounds.
- Existing AlexNet checkpoints, API defaults and saved-model bot selection remain compatible; only compatible AlexNet artifacts can populate that bot selector.
- Account-owned paths and registry metadata determine loading; clients cannot supply arbitrary checkpoint paths.
- CPU tree training must work without PyTorch. Missing LightGBM affects only its family.
- Changing tabs preserves drafts and cannot orphan an account's active training/cancellation.

## Tasks

- [x] Implement Transformer model and numerical tests in `transformer_model.py` / `tests/test_transformer_model.py`: teacher forcing, causal decoding, recursive metrics, cancellation, safe checkpoint round trip and valid ten-candle output.
- [x] Wrap the existing PriceAI, Agentic AI and spot HGB/LightGBM pipelines in `trainer_adapters.py` / `tests/test_trainer_adapters.py`: actual training, purged evaluation, model persistence and family-specific predictions.
- [x] Add the model catalog and architecture validation; update `model_trainer.py` and `server.py` to dispatch training/persistence/prediction. Extend manager/API tests for family availability, defaults, saved metadata and isolation.
- [x] Implement family sub-tabs, controls, draft/job/model filters and OHLCV chart/volume/table preview in `static/index.html`; extend `tests/model_trainer_ui.test.cjs` for switching, races, availability and bot compatibility.
- [x] Update trainer documentation; run Python numerical/lifecycle/API checks and JavaScript UI regressions. Independently review the final integration and repair actionable findings.

## Verification commands

```powershell
python -m pytest tests/test_transformer_model.py tests/test_trainer_adapters.py tests/test_trainer_catalog.py tests/test_model_trainer.py tests/test_alexnet_model.py tests/test_price_ai.py tests/test_agentic_ai.py tests/test_spot_research_integration.py tests/test_auth.py -q --basetemp tmp/trainer-final-suite
node tests/model_trainer_ui.test.cjs
node tests/dashboard_ui.test.cjs
node tests/portfolio_manager_ui.test.cjs
```

## Final verification

- Python: 175 passed, including actual CUDA training followed by CPU checkpoint inference and a Nobitex Tehran-midnight daily-candle regression.
- Trainer UI: 35 passed; Portfolio Manager UI: 34 passed.
- Dashboard UI: 20 passed, two optimizer-workload checks fail because unrelated Sobol population rounding changed estimates from 2,480/1,240 to 3,968/1,984. Existing optimizer changes and expectations were preserved.
- Browser: confirmed all six family tabs, CPU controls, family-scoped dependency feedback, and the 100-observed/10-forecast candlestick, volume and numeric table using synthetic data. Screenshot: `output/model-trainer-transformer.png`.
- Independent review fixes: accepted starts survive refresh/navigation without crossing authentication sessions; unpublished cancelled artifacts are removed; valid Nobitex daily candles retain their exchange calendar.
- LightGBM is absent from the tested Python environment; its sub-tab reports the dependency requirement. CPU HGB, PriceAI, Agentic AI and neural training remain available independently.
- `git diff --check` passed. Direct Node invocations avoid the sandbox's test-runner child-process restriction.
