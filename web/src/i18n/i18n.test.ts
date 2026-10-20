// 覆盖 i18n 键完整性：de/en/zh 键集一致、无空值、占位符一致、源码中引用的键全部存在、枚举派生键全覆盖
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { de } from './de';
import { en } from './en';
import { zh } from './zh';
import { DEFAULT_LOCALE, translate } from './index';
import type { ExclusionReason, GroupBlocker } from '../domain/batch';
import { PIPELINE, SIDE_STATES } from '../domain/requestStatus';

const dicts = { de, en, zh } as Record<string, Record<string, string>>;
const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((f: string) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? walk(p) : /\.(tsx?|ts)$/.test(f) && !f.endsWith('.test.ts') ? [p] : [];
  });
}

describe('i18n completeness', () => {
  const deKeys = Object.keys(de).sort();

  it('default locale is German', () => {
    expect(DEFAULT_LOCALE).toBe('de');
  });

  for (const [name, dict] of Object.entries(dicts)) {
    it(`${name}: same key set as de, no empty values, same placeholders`, () => {
      expect(Object.keys(dict).sort()).toEqual(deKeys);
      for (const k of deKeys) {
        expect(dict[k].trim().length, `${name}.${k} empty`).toBeGreaterThan(0);
        expect(placeholders(dict[k]), `${name}.${k} placeholders`).toEqual(placeholders((de as Record<string, string>)[k]));
      }
    });
  }

  it('every literal t("…") key used in the source exists', () => {
    const srcDir = join(dirname(fileURLToPath(import.meta.url)), '..');
    const missing: string[] = [];
    for (const f of walk(srcDir)) {
      const text = readFileSync(f, 'utf8');
      for (const m of text.matchAll(/\bt\(\s*'([a-zA-Z0-9_.]+)'/g)) if (!(m[1] in de)) missing.push(`${f}: ${m[1]}`);
    }
    expect(missing).toEqual([]);
  });

  it('enum-derived keys are covered', () => {
    const reasons: ExclusionReason[] = ['l3_single', 'legal_commitment', 'level_mismatch', 'not_pending', 'not_reused', 'open_blocking_conflict', 'open_conflict', 'open_warning_needs_note', 'evidence_invalid_at_due', 'evidence_not_verified', 'evidence_expiring_soon', 'stale_version', 'detail_view_required', 'estimate_ack_required'];
    const blockers: GroupBlocker[] = ['batch_not_allowed', 'role', 'mixed_requests', 'group_not_expanded', 'empty'];
    const keys = [
      ...reasons.map((r) => `batch.reason.${r}`),
      ...blockers.map((b) => `batch.blocker.${b}`),
      ...[...PIPELINE, ...SIDE_STATES].map((s) => `status.request.${s}`),
      ...['draft', 'in_review', 'approved', 'rejected', 'submitted', 'superseded'].map((s) => `status.answer.${s}`),
      ...['open', 'resolved', 'accepted_divergence', 'dismissed'].map((s) => `status.conflict.${s}`),
      ...['draft', 'sent', 'reminded', 'responded', 'reviewed', 'closed', 'escalated', 'expired'].map((s) => `status.sreq.${s}`),
      ...['candidate', 'verified', 'expired', 'superseded', 'rejected'].map((s) => `status.evidence.${s}`),
      ...['yes_no', 'single_choice', 'numeric_confirm', 'file_request'].map((k) => `cards.kind.${k}`),
      ...['L0', 'L1', 'L2', 'L3'].map((l) => `risk.${l}`),
      ...['compliance_owner', 'executive', 'sales', 'contributor', 'supplier_user', 'customer_buyer', 'org_admin', 'auditor_readonly'].map((r) => `role.${r}`),
      ...['scopes_overlap', 'requirement_refs_missing', 'note_too_short', 'review_by_missing', 'review_by_too_late', 'disclosure_missing', 'basis_missing'].map((e) => `conflicts.err.${e}`),
      ...['expired', 'd7', 'd14', 'd30', 'd60', 'd90', 'ok'].map((x) => `expiry.tier.${x}`),
    ];
    expect(keys.filter((k) => !(k in de))).toEqual([]);
  });

  it('interpolates variables and falls back to the key', () => {
    expect(translate('de', 'batch.staged', { n: 61, time: '9:59' })).toBe('61 Antworten freigegeben. Rückgängig (9:59)');
    expect(translate('en', 'inbox.empty', { title: 'X', date: '2026-12-15' })).toBe('All caught up. Next deadline: X on 2026-12-15.');
  });
});
