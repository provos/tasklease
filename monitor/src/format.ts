const numbers = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

export function count(value: number): string {
  return numbers.format(value);
}

export function duration(milliseconds: number | null): string {
  if (milliseconds === null || !Number.isFinite(milliseconds)) return "—";
  const seconds = Math.max(0, milliseconds) / 1000;
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${count(seconds)} s`;
  if (seconds < 3600) return `${count(seconds / 60)} min`;
  if (seconds < 86400) return `${count(seconds / 3600)} h`;
  return `${count(seconds / 86400)} d`;
}

export function age(timestamp: number | null, now: number): string {
  return timestamp === null ? "—" : `${duration(now - timestamp)} ago`;
}

export function dateTime(timestamp: number | null): string {
  return timestamp === null ? "—" : new Date(timestamp).toLocaleString();
}

export function hourLabel(timestamp: number): string {
  return new Date(timestamp).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
  });
}

export function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
