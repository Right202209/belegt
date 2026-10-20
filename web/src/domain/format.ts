// Spec: 01 §5.4（数字与日期本地化：de 1.284.300,5 / 31.12.2026；en 1,284,300.5 / 2026-12-31）；03 §2.2 DEC-STR
// 不经浮点：直接对十进制字符串分组，避免精度损失。
export type Locale = 'de' | 'en' | 'zh';

const SEP: Record<Locale, { group: string; decimal: string }> = {
  de: { group: '.', decimal: ',' },
  en: { group: ',', decimal: '.' },
  zh: { group: ',', decimal: '.' },
};

export function formatDec(dec: string, locale: Locale): string {
  if (!/^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(dec)) return dec; // 非 DEC-STR 原样
  const neg = dec.startsWith('-');
  const [int, frac] = (neg ? dec.slice(1) : dec).split('.');
  const { group, decimal } = SEP[locale];
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, group);
  return (neg ? '-' : '') + grouped + (frac ? decimal + frac : '');
}

export function formatDate(iso: string | undefined, locale: Locale): string {
  if (!iso) return '—';
  const d = iso.slice(0, 10);
  if (locale === 'de') return `${d.slice(8, 10)}.${d.slice(5, 7)}.${d.slice(0, 4)}`;
  return d;
}

export function formatDateTime(iso: string | undefined, locale: Locale): string {
  if (!iso) return '—';
  const time = iso.slice(11, 16);
  return `${formatDate(iso, locale)} ${time} UTC`;
}

export function formatPeriod(p: { start: string; end: string } | undefined, locale: Locale): string {
  if (!p) return '—';
  return `${formatDate(p.start, locale)} – ${formatDate(p.end, locale)}`;
}

export function shortHash(h: string | undefined): string {
  if (!h) return '—';
  const v = h.replace(/^sha256:/, '');
  return `sha256:${v.slice(0, 8)}…${v.slice(-4)}`;
}

export function formatEur(dec: string, locale: Locale): string {
  return `€ ${formatDec(dec, locale)}`;
}

/** 01 §5.4：按当前语言解析数字输入；歧义（如 "1.284"）返回两种读法。 */
export function parseLocalizedNumber(input: string, locale: Locale): { value?: string; ambiguous?: [string, string] } {
  const s = input.trim().replace(/\s/g, '');
  if (!s) return {};
  const group = locale === 'de' ? '.' : ',';
  const dec = locale === 'de' ? ',' : '.';
  if (/^\d{1,3}[.,]\d{3}$/.test(s)) {
    const asInt = s.replace(/[.,]/, '');
    const asDec = s.replace(',', '.');
    return s.includes(group) ? { ambiguous: [asInt, asDec] } : { value: s.replace(dec, '.') };
  }
  const normalized = s.split(group).join('').replace(dec, '.');
  return /^-?(0|[1-9]\d*)(\.\d+)?$/.test(normalized) ? { value: normalized } : {};
}

