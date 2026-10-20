// Spec: 06 §3.2（三种处理选项）、§3.3（accepted_divergence / ADR/1 硬性要求）；03 §3.9；01 §3.3
import type { Divergence, Period } from '../types';
import { addMonths } from './dates';

export function scopesOverlap(a: string[] | 'all', b: string[] | 'all'): string[] {
  if (a === 'all' || b === 'all') return ['all'];
  const sb = new Set(b);
  return a.filter((x) => sb.has(x));
}

export type DivergenceError =
  | 'scopes_overlap'
  | 'requirement_refs_missing'
  | 'note_too_short'
  | 'review_by_missing'
  | 'review_by_too_late'
  | 'disclosure_missing'
  | 'basis_missing';

/**
 * 校验接受差异表单：两侧 customer_scope 互斥；≥1 条客户要求依据；理由 ≥ 20 字符（01 §2.2）；
 * review_by 不晚于两侧答案 period.end 之后 15 个月（06 §3.3 #4）；披露文本至少一种语言。
 */
export function validateDivergence(d: Divergence, note: string, sidePeriods: Period[]): DivergenceError[] {
  const errs: DivergenceError[] = [];
  if (d.sides.length >= 2) {
    for (let i = 0; i < d.sides.length; i++)
      for (let j = i + 1; j < d.sides.length; j++)
        if (scopesOverlap(d.sides[i].customer_scope, d.sides[j].customer_scope).length) errs.push('scopes_overlap');
  }
  if (d.customer_requirement_refs.length === 0) errs.push('requirement_refs_missing');
  if (note.trim().length < 20) errs.push('note_too_short');
  if (!d.review_by) errs.push('review_by_missing');
  else if (sidePeriods.length) {
    const earliestEnd = sidePeriods.map((p) => p.end).sort()[0];
    if (d.review_by > addMonths(earliestEnd, 15)) errs.push('review_by_too_late');
  }
  if (!Object.values(d.disclosure_text).some((v) => v && v.trim())) errs.push('disclosure_missing');
  if (d.sides.some((s) => !s.basis.trim())) errs.push('basis_missing');
  return [...new Set(errs)];
}
