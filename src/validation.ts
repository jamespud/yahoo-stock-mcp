const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

export function isValidIsoDate(value: string): boolean {
  const match = ISO_DATE_RE.exec(value);
  if (!match) return false;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const ms = Date.UTC(year, month - 1, day);
  const date = new Date(ms);

  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

export function isoDateToUnixSeconds(value: string): number {
  if (!isValidIsoDate(value)) {
    throw new Error(`invalid ISO date: ${value}`);
  }
  return Math.floor(Date.parse(`${value}T00:00:00Z`) / 1000);
}
