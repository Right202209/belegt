// 覆盖 01 §5.4 数字/日期本地化与歧义输入；06 §3.3 divergence 校验
import { describe, expect, it } from 'vitest';
import { formatDate, formatDec, parseLocalizedNumber } from './format';
import { validateDivergence, scopesOverlap } from './conflict';
import { expiryTier, tierSeverity } from './expiry';
import { parseStatusReason, pipelineIndex } from './requestStatus';

describe('format', () => {
  it('formats DEC-STR without float loss', () => {
    expect(formatDec('1284300.5', 'de')).toBe('1.284.300,5');
    expect(formatDec('1284300.5', 'en')).toBe('1,284,300.5');
    expect(formatDec('-0.000000000000000001', 'de')).toBe('-0,000000000000000001');
    expect(formatDec('12345678901234567.8', 'en')).toBe('12,345,678,901,234,567.8');
    expect(formatDate('2026-12-31', 'de')).toBe('31.12.2026');
    expect(formatDate('2026-12-31', 'en')).toBe('2026-12-31');
  });
  it('parses localized input and flags ambiguity', () => {
    expect(parseLocalizedNumber('1.284.300,5', 'de')).toEqual({ value: '1284300.5' });
    expect(parseLocalizedNumber('1,284,300.5', 'en')).toEqual({ value: '1284300.5' });
    expect(parseLocalizedNumber('1.284', 'de')).toEqual({ ambiguous: ['1284', '1.284'] });
    expect(parseLocalizedNumber('abc', 'de')).toEqual({});
  });
});

describe('conflict divergence (ADR/1)', () => {
  const base = {
    format: 'ADR/1' as const,
    reason_code: 'consolidation' as const,
    customer_requirement_refs: ['q_1'],
    sides: [
      { answer: 'ans_a#v1', customer_scope: ['cp_a'], basis: 'operational_control' },
      { answer: 'ans_b#v1', customer_scope: ['cp_b'], basis: 'equity_share' },
    ],
    disclosure_text: { de: 'Unterschiedliche Konsolidierung gemäß Kundenvorgabe.' },
    include_in_export: true,
    review_by: '2026-12-31',
  };
  const periods = [{ start: '2025-01-01', end: '2025-12-31' }];
  it('accepts a valid record', () => {
    expect(validateDivergence(base, 'Kunde verlangt Equity-Share-Methode.', periods)).toEqual([]);
  });
  it('rejects overlapping scopes, short note and late review date', () => {
    const bad = { ...base, sides: [base.sides[0], { ...base.sides[1], customer_scope: ['cp_a'] }], review_by: '2027-06-01' };
    expect(validateDivergence(bad, 'kurz', periods).sort()).toEqual(['note_too_short', 'review_by_too_late', 'scopes_overlap']);
    expect(scopesOverlap('all', ['cp_a'])).toEqual(['all']);
  });
});

describe('expiry tiers and request status', () => {
  it('maps days to 06 §7 tiers', () => {
    expect(expiryTier('2026-10-19', '2026-10-19')).toBe('expired');
    expect(expiryTier('2026-10-24', '2026-10-19')).toBe('d7');
    expect(expiryTier('2026-11-18', '2026-10-19')).toBe('d30');
    expect(tierSeverity('d30')).toBe('warn');
    expect(tierSeverity('d60')).toBe('info');
    expect(expiryTier(undefined, '2026-10-19')).toBe('ok');
  });
  it('parses status_reason return_to', () => {
    expect(parseStatusReason('awaiting_human:conflict;return_to=auditing')).toEqual({ kind: 'awaiting_human', detail: 'conflict', return_to: 'auditing' });
    expect(pipelineIndex('awaiting_human', 'awaiting_human:conflict;return_to=auditing')).toBe(6);
  });
});
