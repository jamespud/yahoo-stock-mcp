# Typed DSL guide

The deployed `quant_get_capabilities.dsl_schema` is authoritative. Schema version is `1.0`.
A strategy has name, decision_timeframe, boolean entry/exit, optional named signals and numeric
parameter definitions (value_type, default, minimum, maximum). Unknown fields are rejected.

| Expression | Fields | Meaning |
| --- | --- | --- |
| const | kind, value | Strict boolean or finite numeric literal |
| ref | kind, symbol, field, timeframe, adjustment | Raw or split_asof OHLCV; `$symbol` binds at compile/evaluation |
| param | kind, name | Numeric declared parameter |
| signal | kind, name | Named signal dependency; cycles are invalid |
| binary | kind, op, left, right | Typed arithmetic/comparison or boolean combination |
| call | kind, operator, args, params | Deployed operator and bounded parameters |
| align | kind, value, timeframe, policy | Explicit `last_completed` alignment to a target grid |

Use comparisons to turn numeric indicator results into booleans. Operator spelling and versions
come from capabilities; do not invent `cross_above` or execution fields in a DSL expression.
For EMA/RSI, call the registered `ema`/`rsi` operator with `params.period` and a price ref argument.
Source warmup and target-grid warmup after alignment differ. Binding a symbol does not acquire data.

The adjacent strategy-examples.json contains full validation requests and one deliberate numeric
entry rejection. Run those requests through `quant_validate_strategy`; do not execute the invalid
example. Positive examples cover EMA/RSI, explicit multi-timeframe alignment and parameter binding.

## Complete validation requests

Each JSON block is a quant_validate_strategy argument object. The last block must be rejected
with TYPE_MISMATCH; the others must validate successfully. These blocks are exercised by the live
quant skill test, alongside the adjacent machine-readable fixtures.

### ema-rsi

```json
{
  "strategy": {
    "schema_version": "1.0",
    "name": "ema-rsi",
    "decision_timeframe": "1d",
    "entry": {
      "kind": "binary",
      "op": "and",
      "left": {
        "kind": "binary",
        "op": "gt",
        "left": {
          "kind": "ref",
          "symbol": "$symbol",
          "field": "close",
          "timeframe": "1d"
        },
        "right": {
          "kind": "call",
          "operator": "ema",
          "args": [
            {
              "kind": "ref",
              "symbol": "$symbol",
              "field": "close",
              "timeframe": "1d"
            }
          ],
          "params": {
            "period": 5
          }
        }
      },
      "right": {
        "kind": "binary",
        "op": "lt",
        "left": {
          "kind": "call",
          "operator": "rsi",
          "args": [
            {
              "kind": "ref",
              "symbol": "$symbol",
              "field": "close",
              "timeframe": "1d"
            }
          ],
          "params": {
            "period": 3
          }
        },
        "right": {
          "kind": "const",
          "value": 70
        }
      }
    },
    "exit": {
      "kind": "binary",
      "op": "lt",
      "left": {
        "kind": "ref",
        "symbol": "$symbol",
        "field": "close",
        "timeframe": "1d"
      },
      "right": {
        "kind": "call",
        "operator": "ema",
        "args": [
          {
            "kind": "ref",
            "symbol": "$symbol",
            "field": "close",
            "timeframe": "1d"
          }
        ],
        "params": {
          "period": 5
        }
      }
    }
  },
  "symbol": "AAPL"
}
```

### bound-parameters

```json
{
  "strategy": {
    "schema_version": "1.0",
    "name": "bound-ema",
    "decision_timeframe": "1d",
    "entry": {
      "kind": "binary",
      "op": "gt",
      "left": {
        "kind": "ref",
        "symbol": "$symbol",
        "field": "close",
        "timeframe": "1d"
      },
      "right": {
        "kind": "call",
        "operator": "ema",
        "args": [
          {
            "kind": "ref",
            "symbol": "$symbol",
            "field": "close",
            "timeframe": "1d"
          }
        ],
        "params": {
          "period": {
            "kind": "param",
            "name": "period"
          }
        }
      }
    },
    "exit": {
      "kind": "binary",
      "op": "lt",
      "left": {
        "kind": "ref",
        "symbol": "$symbol",
        "field": "close",
        "timeframe": "1d"
      },
      "right": {
        "kind": "call",
        "operator": "ema",
        "args": [
          {
            "kind": "ref",
            "symbol": "$symbol",
            "field": "close",
            "timeframe": "1d"
          }
        ],
        "params": {
          "period": 5
        }
      }
    },
    "parameters": {
      "period": {
        "value_type": "integer",
        "default": 5,
        "minimum": 2,
        "maximum": 20
      }
    }
  },
  "symbol": "AAPL",
  "parameters": {
    "period": 8
  }
}
```

### explicit-alignment

```json
{
  "strategy": {
    "schema_version": "1.0",
    "name": "completed-daily-on-hourly",
    "decision_timeframe": "1h",
    "entry": {
      "kind": "binary",
      "op": "gt",
      "left": {
        "kind": "align",
        "value": {
          "kind": "call",
          "operator": "ema",
          "args": [
            {
              "kind": "ref",
              "symbol": "$symbol",
              "field": "close",
              "timeframe": "1d"
            }
          ],
          "params": {
            "period": 5
          }
        },
        "timeframe": "1h",
        "policy": "last_completed"
      },
      "right": {
        "kind": "const",
        "value": 10
      }
    },
    "exit": {
      "kind": "const",
      "value": false
    }
  },
  "symbol": "AAPL"
}
```

### numeric-entry-rejected

```json
{
  "strategy": {
    "schema_version": "1.0",
    "name": "ema-rsi",
    "decision_timeframe": "1d",
    "entry": {
      "kind": "const",
      "value": 1
    },
    "exit": {
      "kind": "binary",
      "op": "lt",
      "left": {
        "kind": "ref",
        "symbol": "$symbol",
        "field": "close",
        "timeframe": "1d"
      },
      "right": {
        "kind": "call",
        "operator": "ema",
        "args": [
          {
            "kind": "ref",
            "symbol": "$symbol",
            "field": "close",
            "timeframe": "1d"
          }
        ],
        "params": {
          "period": 5
        }
      }
    }
  },
  "symbol": "AAPL"
}
```
