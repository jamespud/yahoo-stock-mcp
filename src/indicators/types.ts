export type Series = Array<number | null>;

export type Field = "open" | "high" | "low" | "close" | "adjClose" | "volume";

export type IndicatorGroup =
  | "overlap"
  | "momentum"
  | "oscillator"
  | "volume"
  | "volatility"
  | "price"
  | "regression";

export interface IndicatorBar {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  adjClose: number | null;
  volume: number | null;
}

export interface ParamSpec {
  name: string;
  default: number;
  min: number;
  max: number;
  integer: boolean;
  description: string;
}

export type Params = Record<string, number>;

export interface IndicatorSpec {
  name: string;
  group: IndicatorGroup;
  summary: string;
  params: ParamSpec[];
  outputs: string[];
  requires: Field[];
  lookback(p: Params): number;
  calculate(bars: IndicatorBar[], p: Params): Series[];
}

export interface IndicatorModule {
  group: IndicatorGroup;
  indicators: IndicatorSpec[];
}

export function num(
  name: string,
  def: number,
  min: number,
  max: number,
  description: string,
  integer = true
): ParamSpec {
  return { name, default: def, min, max, integer, description };
}

export function defineIndicators(
  group: IndicatorGroup,
  specs: Array<Omit<IndicatorSpec, "group">>
): IndicatorModule {
  return { group, indicators: specs.map((s) => ({ ...s, group })) };
}
