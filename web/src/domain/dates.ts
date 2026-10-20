// Spec: 03 §2（日期 ISO，时间 UTC）
export function toIsoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** 两个 ISO 日期之间的整天数（b − a）。 */
export function daysBetween(a: string, b: string): number {
  const ta = Date.UTC(+a.slice(0, 4), +a.slice(5, 7) - 1, +a.slice(8, 10));
  const tb = Date.UTC(+b.slice(0, 4), +b.slice(5, 7) - 1, +b.slice(8, 10));
  return Math.round((tb - ta) / 86_400_000);
}

export function addMonths(iso: string, months: number): string {
  const d = new Date(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1 + months, +iso.slice(8, 10)));
  return toIsoDate(d);
}
