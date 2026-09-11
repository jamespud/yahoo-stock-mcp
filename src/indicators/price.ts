import { pick, zipAll } from "./math.js";
import { defineIndicators } from "./types.js";

export const PRICE = defineIndicators("price", [
  {
    name: "TYPPRICE",
    summary: "典型价 (high + low + close) / 3，CCI、MFI、VWAP 的常用输入。",
    params: [],
    outputs: ["typprice"],
    requires: ["high", "low", "close"],
    lookback: () => 0,
    calculate: (bars) => [
      zipAll([pick(bars, "high"), pick(bars, "low"), pick(bars, "close")], ([h, l, c]) => (h + l + c) / 3),
    ],
  },
  {
    name: "MEDPRICE",
    summary: "中位价 (high + low) / 2，Awesome Oscillator 的输入。",
    params: [],
    outputs: ["medprice"],
    requires: ["high", "low"],
    lookback: () => 0,
    calculate: (bars) => [zipAll([pick(bars, "high"), pick(bars, "low")], ([h, l]) => (h + l) / 2)],
  },
  {
    name: "WCLPRICE",
    summary: "加权收盘价 (high + low + 2 × close) / 4。",
    params: [],
    outputs: ["wclprice"],
    requires: ["high", "low", "close"],
    lookback: () => 0,
    calculate: (bars) => [
      zipAll([pick(bars, "high"), pick(bars, "low"), pick(bars, "close")], ([h, l, c]) => (h + l + 2 * c) / 4),
    ],
  },
  {
    name: "AVGPRICE",
    summary: "平均价 (open + high + low + close) / 4。",
    params: [],
    outputs: ["avgprice"],
    requires: ["open", "high", "low", "close"],
    lookback: () => 0,
    calculate: (bars) => [
      zipAll(
        [pick(bars, "open"), pick(bars, "high"), pick(bars, "low"), pick(bars, "close")],
        ([o, h, l, c]) => (o + h + l + c) / 4
      ),
    ],
  },
]);
