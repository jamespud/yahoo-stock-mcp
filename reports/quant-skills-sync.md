# Quant skill sync and validation

9 independently installable skills; 2 quant skills; 13 synchronized references.

Hash algorithm: SHA-256. Reference status: MATCH. NOT_TESTED means no successful observation for this content fingerprint. OPT_IN_NOT_EXPOSED is the expected default quant-skill status.

| Skill | Source hash | Bundled hash | Schema | Default mode | Quant mode | References | Plugin |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dividend-research | 0cb1692395bf1b922c8f9ddd2f6c8db77596c8ece66450aa2790bd068b276c19 | 0cb1692395bf1b922c8f9ddd2f6c8db77596c8ece66450aa2790bd068b276c19 | PASS | PASS | PASS | PASS | PASS |
| earnings-event-research | a3cd062fab6671cb380677d69338b810b839cd8cf21ed1213b5e9768fce14f04 | a3cd062fab6671cb380677d69338b810b839cd8cf21ed1213b5e9768fce14f04 | PASS | PASS | PASS | PASS | PASS |
| options-analysis | d2480fbe7f2dac1409dcff3dde77cd77e5e186d6f413111b4039eae6642e728c | d2480fbe7f2dac1409dcff3dde77cd77e5e186d6f413111b4039eae6642e728c | PASS | PASS | PASS | PASS | PASS |
| quant-research-reliability | ff0a0d28f9041fa062a1070862d4c33c57b5bc69141cf3387b312704fe2187f7 | ff0a0d28f9041fa062a1070862d4c33c57b5bc69141cf3387b312704fe2187f7 | PASS | OPT_IN_NOT_EXPOSED | PASS | PASS | PASS |
| quant-strategy-research | 1306a004254e2da0cca4acb67e8e241b474fddb79fdc8119f8624bf835089461 | 1306a004254e2da0cca4acb67e8e241b474fddb79fdc8119f8624bf835089461 | PASS | OPT_IN_NOT_EXPOSED | PASS | PASS | PASS |
| sector-rotation | 3ac26f2001c02d215f65935bff4d4b3261e22dda35972fa0bf65ce1175517ab0 | 3ac26f2001c02d215f65935bff4d4b3261e22dda35972fa0bf65ce1175517ab0 | PASS | PASS | PASS | PASS | PASS |
| stock-data-setup | c06c7b3b33b436da748d2810168b68c6c92f77a1b67af1307b9a2445ddf3b06c | c06c7b3b33b436da748d2810168b68c6c92f77a1b67af1307b9a2445ddf3b06c | PASS | PASS | PASS | PASS | PASS |
| stock-research | 0845a236216d6dbf5535ab7c344a32bdc29825085047b569970d8f613a7f0e6a | 0845a236216d6dbf5535ab7c344a32bdc29825085047b569970d8f613a7f0e6a | PASS | PASS | PASS | PASS | PASS |
| technical-analysis | 4ae3cffa44fdf7d88a30307b49a2459688f00053a33b1fadabeb99b86018acc2 | 4ae3cffa44fdf7d88a30307b49a2459688f00053a33b1fadabeb99b86018acc2 | PASS | PASS | PASS | PASS | PASS |

## Tool resolution

- dividend-research: declared get_dividends, get_ratios, get_financials, get_company_events, get_quote, sync_stock; resolved get_dividends, get_ratios, get_financials, get_company_events, get_quote, sync_stock.
- earnings-event-research: declared get_company_events, get_earnings, get_earnings_trend, get_analyst_actions, get_recommendation_trend, get_analyst_forecast, get_quote, sync_stock; resolved get_company_events, get_earnings, get_earnings_trend, get_analyst_actions, get_recommendation_trend, get_analyst_forecast, get_quote, sync_stock.
- options-analysis: declared get_option_quote, get_options, get_quote, sync_stock, get_bars; resolved get_option_quote, get_options, get_quote, sync_stock, get_bars.
- quant-research-reliability: declared quant_get_capabilities, quant_validate_strategy, quant_register_strategy, quant_compile_strategy, quant_get_data_quality, quant_run_backtest, quant_get_job, quant_cancel_job, quant_get_result; resolved quant_get_capabilities, quant_validate_strategy, quant_register_strategy, quant_compile_strategy, quant_get_data_quality, quant_run_backtest, quant_get_job, quant_cancel_job, quant_get_result.
- quant-strategy-research: declared quant_get_capabilities, quant_validate_strategy, quant_register_strategy, quant_compile_strategy, quant_get_calendar, quant_import_snapshot, quant_snapshot_from_local, quant_get_data_quality, quant_evaluate_signal, quant_run_screen, quant_optimize, quant_run_backtest, quant_get_job, quant_cancel_job, quant_get_result; resolved quant_get_capabilities, quant_validate_strategy, quant_register_strategy, quant_compile_strategy, quant_get_calendar, quant_import_snapshot, quant_snapshot_from_local, quant_get_data_quality, quant_evaluate_signal, quant_run_screen, quant_optimize, quant_run_backtest, quant_get_job, quant_cancel_job, quant_get_result.
- sector-rotation: declared list_sectors, get_sector_performance, get_sector_members, get_bars, sync_sectors; resolved list_sectors, get_sector_performance, get_sector_members, get_bars, sync_sectors.
- stock-data-setup: declared sync_stock, sync_sectors, search_symbol, get_quote, get_sector_performance; resolved sync_stock, sync_sectors, search_symbol, get_quote, get_sector_performance.
- stock-research: declared search_symbol, get_quote, get_profile, get_ratios, get_financials, get_news, sync_stock; resolved search_symbol, get_quote, get_profile, get_ratios, get_financials, get_news, sync_stock.
- technical-analysis: declared list_indicators, get_bars, get_indicators, get_intraday_bars, sync_stock, search_symbol; resolved list_indicators, get_bars, get_indicators, get_intraday_bars, sync_stock, search_symbol.

## Shared references

| Source | Copy | Source SHA-256 | Copy SHA-256 | Matches |
| --- | --- | --- | --- | --- |
| skills/references/data-policy.md | skills/dividend-research/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/data-policy.md | skills/earnings-event-research/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/data-policy.md | skills/options-analysis/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/data-policy.md | skills/quant-research-reliability/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/quant-research-policy.md | skills/quant-research-reliability/references/quant-research-policy.md | 7114656c7a0afa4c011ac2e94700271978029e744df679ae1aa9a9cfb1471971 | 7114656c7a0afa4c011ac2e94700271978029e744df679ae1aa9a9cfb1471971 | true |
| skills/references/strategy-examples.json | skills/quant-research-reliability/references/strategy-examples.json | 6b721f5ebcca59964987e9d362331d2f1070557cf6657b6ca5f48ebb58e956fc | 6b721f5ebcca59964987e9d362331d2f1070557cf6657b6ca5f48ebb58e956fc | true |
| skills/references/data-policy.md | skills/quant-strategy-research/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/quant-research-policy.md | skills/quant-strategy-research/references/quant-research-policy.md | 7114656c7a0afa4c011ac2e94700271978029e744df679ae1aa9a9cfb1471971 | 7114656c7a0afa4c011ac2e94700271978029e744df679ae1aa9a9cfb1471971 | true |
| skills/references/strategy-examples.json | skills/quant-strategy-research/references/strategy-examples.json | 6b721f5ebcca59964987e9d362331d2f1070557cf6657b6ca5f48ebb58e956fc | 6b721f5ebcca59964987e9d362331d2f1070557cf6657b6ca5f48ebb58e956fc | true |
| skills/references/data-policy.md | skills/sector-rotation/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/data-policy.md | skills/stock-data-setup/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/data-policy.md | skills/stock-research/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
| skills/references/data-policy.md | skills/technical-analysis/references/data-policy.md | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | 5fa328c84a609731fda7d05ade4692fa9ebfc7b1bc6d96e02a2f9e7c91abff9c | true |
