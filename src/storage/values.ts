/**
 * Value contracts for the SQLite storage boundary (v0.5.0, C2).
 *
 * Three concerns live here because they all answer the same question — "how does a value cross
 * the JavaScript/SQLite boundary without changing meaning?":
 *
 *   1. DECIMAL  — exact decimal strings; a float must never be reached by accident.
 *   2. DATETIME — system time is UTC, upstream times are converted explicitly, DATE is date-only.
 *   3. BINDING  — `node:sqlite` accepts only null/number/bigint/string/Uint8Array.
 *
 * See docs/SQLITE_MIGRATION_SPEC.md §3.4 for the contract these functions implement.
 */

// ---------------------------------------------------------------------------------------------
// shared helpers
// ---------------------------------------------------------------------------------------------

function describeValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "bigint") return `${value}n`;
  if (typeof value === "number" && Number.isNaN(value)) return "NaN";
  if (typeof value === "object") return Object.prototype.toString.call(value);
  return `${typeof value} ${String(value)}`;
}

// ---------------------------------------------------------------------------------------------
// 1. DECIMAL — exact decimal strings
// ---------------------------------------------------------------------------------------------

/** Canonical decimal literal: an optional sign, digits, and an optional fractional part. */
export const DECIMAL_STRING_RE = /^-?\d+(\.\d+)?$/;

export function isDecimalString(value: unknown): value is string {
  return typeof value === "string" && DECIMAL_STRING_RE.test(value);
}

export function assertDecimalString(value: unknown, context = "value"): string {
  if (!isDecimalString(value)) {
    throw new TypeError(
      `${context} must be an exact decimal string (e.g. "10.5000"); received ${describeValue(value)}`
    );
  }
  return value;
}

/**
 * Validate an exact decimal string for storage. Returns null for null/undefined.
 *
 * A JavaScript `number` is **rejected**: by the time the value is a number the precision may
 * already be gone, so the lossy conversion has to be named explicitly at the call site via
 * `decimalFromNumber`. `bigint` is accepted because it is exact.
 */
export function toDecimalString(
  value: string | bigint | null | undefined,
  context = "value"
): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "bigint") return value.toString();
  const raw: unknown = value;
  if (typeof raw === "number") {
    throw new TypeError(
      `${context} must be an exact decimal string; a JS number may already have lost precision. ` +
        `Call decimalFromNumber(value) if that float conversion is intended.`
    );
  }
  return assertDecimalString(raw, context);
}

/**
 * The explicit, documented bridge from a JavaScript float to a decimal string.
 *
 * Numbers that stringify into exponential notation (1e21, 1e-7, ...) are rejected: they cannot
 * be represented as a plain decimal literal, and silently expanding them would invent digits.
 */
export function decimalFromNumber(value: number, context = "value"): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new TypeError(`${context} must be a finite number; received ${describeValue(value)}`);
  }
  const text = String(value);
  if (!DECIMAL_STRING_RE.test(text)) {
    throw new TypeError(
      `${context}: ${value} stringifies as ${text}, which needs exponential notation. ` +
        `Pass the exact decimal string instead of a number.`
    );
  }
  return text;
}

/**
 * Read a DECIMAL column out of a SQLite row. The column must hold TEXT: reading a JS number
 * means the column is not storing exact decimals, so it is reported rather than trusted.
 */
export function decimalFromStorage(value: unknown, context = "value"): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "bigint") return value.toString();
  if (typeof value === "number") {
    throw new TypeError(
      `${context}: read a JS number from a DECIMAL column (${describeValue(value)}); ` +
        `decimal columns must be stored as TEXT`
    );
  }
  return assertDecimalString(value, context);
}

/**
 * Round a decimal string to `scale` fractional digits, half away from zero.
 *
 * This matches how MySQL rounds an exact value on INSERT into `DECIMAL(p,s)`, verified against
 * MySQL 8.4 (`'1.5'`→`2`, `'-2.5'`→`-3`, `'0.00005'`@4→`0.0001`, `'999.99'`@1→`1000.0`).
 * Implemented with BigInt so no float is ever involved. `-0.0000` normalizes to `0.0000`.
 */
export function quantizeDecimal(value: string, scale: number, context = "value"): string {
  assertDecimalString(value, context);
  if (!Number.isInteger(scale) || scale < 0) {
    throw new TypeError(`${context}: scale must be a non-negative integer; received ${describeValue(scale)}`);
  }

  const negative = value.startsWith("-");
  const body = negative ? value.slice(1) : value;
  const dot = body.indexOf(".");
  const intPart = dot === -1 ? body : body.slice(0, dot);
  const fracPart = dot === -1 ? "" : body.slice(dot + 1);

  let magnitude = BigInt(intPart + fracPart.slice(0, scale).padEnd(scale, "0"));
  if (fracPart.length > scale && fracPart.charCodeAt(scale) - 48 >= 5) {
    magnitude += 1n;
  }

  const sign = negative && magnitude !== 0n ? "-" : "";
  if (scale === 0) return sign + magnitude.toString();

  const digits = magnitude.toString().padStart(scale + 1, "0");
  return `${sign}${digits.slice(0, digits.length - scale)}.${digits.slice(digits.length - scale)}`;
}

/**
 * Convert a stored decimal to a number **at the indicator-calculation boundary only**, where
 * IEEE-754 semantics are explicitly accepted. Do not use this on the persistence path.
 */
export function decimalToNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// ---------------------------------------------------------------------------------------------
// 2. DATETIME — UTC timestamps and date-only values
// ---------------------------------------------------------------------------------------------

/** `YYYY-MM-DD HH:MM:SS`, always UTC. Matches SQLite's `CURRENT_TIMESTAMP`. */
export const UTC_TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
/** `YYYY-MM-DD`, no time and no zone. */
export const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/;

const pad = (n: number, width = 2): string => String(n).padStart(width, "0");

function formatUtc(date: Date): string {
  return (
    `${pad(date.getUTCFullYear(), 4)}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`
  );
}

function formatUtcDate(date: Date): string {
  return `${pad(date.getUTCFullYear(), 4)}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
}

/** Parse `YYYY-MM-DD HH:MM:SS` as UTC (zone-less timestamps are UTC by contract). */
export function parseUtcTimestamp(value: string, context = "value"): Date {
  assertUtcTimestamp(value, context);
  const [datePart, timePart] = value.split(" ");
  const [y, mo, d] = datePart.split("-").map(Number);
  const [h, mi, s] = timePart.split(":").map(Number);
  return new Date(Date.UTC(y, mo - 1, d, h, mi, s));
}

/** True when `value` is a well-formed UTC timestamp that survives a format round-trip. */
export function isUtcTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || !UTC_TIMESTAMP_RE.test(value)) return false;
  const [datePart, timePart] = value.split(" ");
  const [y, mo, d] = datePart.split("-").map(Number);
  const [h, mi, s] = timePart.split(":").map(Number);
  const date = new Date(Date.UTC(y, mo - 1, d, h, mi, s));
  return !Number.isNaN(date.getTime()) && formatUtc(date) === value;
}

export function assertUtcTimestamp(value: unknown, context = "value"): string {
  if (!isUtcTimestamp(value)) {
    throw new TypeError(
      `${context} must be a UTC timestamp "YYYY-MM-DD HH:MM:SS"; received ${describeValue(value)}`
    );
  }
  return value;
}

/** True when `value` is a date-only string that survives a format round-trip. */
export function isDateOnly(value: unknown): value is string {
  if (typeof value !== "string" || !DATE_ONLY_RE.test(value)) return false;
  const [y, mo, d] = value.split("-").map(Number);
  const date = new Date(Date.UTC(y, mo - 1, d));
  return !Number.isNaN(date.getTime()) && formatUtcDate(date) === value;
}

export function assertDateOnly(value: unknown, context = "value"): string {
  if (!isDateOnly(value)) {
    throw new TypeError(`${context} must be a date-only string "YYYY-MM-DD"; received ${describeValue(value)}`);
  }
  return value;
}

/**
 * Normalize any instant to a UTC timestamp. `Date` and epoch-millisecond inputs are converted,
 * not reinterpreted; an existing canonical timestamp passes through unchanged.
 */
export function toUtcTimestamp(value: Date | number | string, context = "value"): string {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) throw new TypeError(`${context}: received an invalid Date`);
    return formatUtc(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError(`${context}: received ${describeValue(value)}`);
    return formatUtc(new Date(value));
  }
  if (typeof value === "string") {
    // Held as `unknown` so the type guards narrow instead of collapsing the else-branch to never.
    const text: unknown = value;
    if (isUtcTimestamp(text)) return text;
    const ms = Date.parse(value);
    if (Number.isNaN(ms)) throw new TypeError(`${context}: not a parseable timestamp: ${describeValue(value)}`);
    return formatUtc(new Date(ms));
  }
  throw new TypeError(`${context}: expected a Date, epoch milliseconds, or a timestamp string; got ${describeValue(value)}`);
}

/**
 * Normalize to a date-only value. A timestamp contributes its **UTC** calendar date, which is
 * why a DATE column must never carry a time or a zone.
 */
export function toDateOnly(value: Date | string, context = "value"): string {
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) throw new TypeError(`${context}: received an invalid Date`);
    return formatUtcDate(value);
  }
  if (typeof value === "string") {
    // Held as `unknown` so the type guards narrow instead of collapsing the else-branch to never.
    const text: unknown = value;
    if (isDateOnly(text)) return text;
    if (isUtcTimestamp(text)) return text.slice(0, 10);
    const ms = Date.parse(value);
    if (Number.isNaN(ms)) throw new TypeError(`${context}: not a date or timestamp: ${describeValue(value)}`);
    return formatUtcDate(new Date(ms));
  }
  throw new TypeError(`${context}: expected a Date or a date string; got ${describeValue(value)}`);
}

/**
 * Convert an upstream **wall-clock** value (no zone information) to UTC using an explicit
 * offset. `offsetMinutes` is the source's offset from UTC, so UTC+8 is `480` (Beijing) and
 * US Eastern standard time is `-300`.
 *
 * The offset is required rather than inferred: MySQL `DATETIME` carries no zone, so guessing
 * (server time zone, or the host's zone) is exactly the kind of implicit change the contract
 * forbids.
 */
export function upstreamToUtc(wallClock: string, offsetMinutes: number, context = "value"): string {
  if (!Number.isFinite(offsetMinutes)) {
    throw new TypeError(`${context}: offsetMinutes must be a finite number; received ${describeValue(offsetMinutes)}`);
  }
  const asUtc = Date.parse(`${wallClock.replace(" ", "T")}Z`);
  if (Number.isNaN(asUtc)) {
    throw new TypeError(`${context}: not a parseable wall-clock timestamp: ${describeValue(wallClock)}`);
  }
  return formatUtc(new Date(asUtc - offsetMinutes * 60_000));
}

/** Inverse of `upstreamToUtc`: render a UTC instant as the source's local wall clock. */
export function utcToUpstream(utcTimestamp: string, offsetMinutes: number, context = "value"): string {
  const ms = parseUtcTimestamp(utcTimestamp, context).getTime();
  if (!Number.isFinite(offsetMinutes)) {
    throw new TypeError(`${context}: offsetMinutes must be a finite number; received ${describeValue(offsetMinutes)}`);
  }
  return formatUtc(new Date(ms + offsetMinutes * 60_000));
}

// ---------------------------------------------------------------------------------------------
// 3. BINDING — the value set `node:sqlite` accepts
// ---------------------------------------------------------------------------------------------

export type SqliteParam = null | number | bigint | string | Uint8Array;

/** A value that may be read back out of an INTEGER column. */
export type IntegerOut = number | string;

/**
 * Normalize a value into something `node:sqlite` can bind, or throw.
 *
 * Booleans are converted explicitly to `0`/`1` — `node:sqlite` rejects `true`/`false` with
 * "Provided value cannot be bound to SQLite parameter" — and `Date` is converted to a UTC
 * timestamp. Anything else (objects, arrays, symbols, functions) is an error rather than a
 * silent stringification.
 */
export function toSqliteParam(value: unknown, context = "parameter"): SqliteParam {
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "bigint" || typeof value === "string") return value;
  if (value instanceof Uint8Array) return value;
  if (value instanceof Date) return toUtcTimestamp(value, context);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError(`${context}: ${describeValue(value)} cannot be bound to SQLite`);
    }
    return value;
  }
  throw new TypeError(`${context}: unsupported SQLite parameter type ${describeValue(value)}`);
}

export function toSqliteParams(values: readonly unknown[], context = "parameter"): SqliteParam[] {
  return values.map((value, index) => toSqliteParam(value, `${context}[${index}]`));
}

/**
 * Enable bigint reads for a single statement and return it.
 *
 * `node:sqlite` raises `RangeError: Value is too large to be represented as a JavaScript number`
 * rather than truncating, so big-integer columns only need this opt-in per statement — the
 * global `readBigInts` option is deliberately not used, because it would turn every id into a
 * `bigint`.
 */
export function statementWithBigInts<T extends { setReadBigInts(value: boolean): void }>(statement: T): T {
  statement.setReadBigInts(true);
  return statement;
}

/**
 * Normalize an integer read out of SQLite for downstream (JSON/MCP) use.
 *
 * Values inside `Number.MAX_SAFE_INTEGER` stay numbers; anything larger becomes a decimal
 * string, because `JSON.stringify` throws on `bigint` and a number would silently lose digits.
 */
export function integerOut(value: number | bigint | null | undefined): IntegerOut | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError(`value ${value} is not a safe integer; read it as bigint first`);
    }
    return value;
  }
  const asNumber = BigInt(Number.MAX_SAFE_INTEGER);
  if (value <= asNumber && value >= -asNumber) return Number(value);
  return value.toString();
}
