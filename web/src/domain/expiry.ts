// Spec: 06 §7（有效期预警梯度 90/60/30/14/7/0 天）；FR-404
import type { Evidence } from '../types';
import { daysBetween } from './dates';

export type ExpiryTier = 'expired' | 'd7' | 'd14' | 'd30' | 'd60' | 'd90' | 'ok';

export function expiryTier(validUntil: string | undefined, todayIso: string): ExpiryTier {
  if (!validUntil) return 'ok';
  const d = daysBetween(todayIso, validUntil);
  if (d <= 0) return 'expired';
  if (d <= 7) return 'd7';
  if (d <= 14) return 'd14';
  if (d <= 30) return 'd30';
  if (d <= 60) return 'd60';
  if (d <= 90) return 'd90';
  return 'ok';
}

export function tierSeverity(t: ExpiryTier): 'block' | 'warn' | 'info' | 'none' {
  if (t === 'expired') return 'block';
  if (t === 'd7' || t === 'd14' || t === 'd30') return 'warn';
  if (t === 'd60' || t === 'd90') return 'info';
  return 'none';
}

export function expiringEvidence(list: Evidence[], todayIso: string): { evidence: Evidence; tier: ExpiryTier; days: number }[] {
  return list
    .filter((e) => e.valid_until && (e.status === 'verified' || e.status === 'expired'))
    .map((e) => ({ evidence: e, tier: expiryTier(e.valid_until, todayIso), days: daysBetween(todayIso, e.valid_until!) }))
    .filter((x) => x.tier !== 'ok')
    .sort((a, b) => a.days - b.days);
}
