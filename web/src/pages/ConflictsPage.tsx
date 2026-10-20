// Spec: 01 §3.3 UI-03 冲突处理；06 §3.2（三种处理选项）、§3.3（ADR/1 divergence）；03 §3.9
import { useEffect, useMemo, useState } from 'react';
import type { ConflictView, Divergence, ReasonCode } from '../types';
import { useApp, useAsync } from '../state/store';
import { pickText, useI18n, type MessageKey } from '../i18n';
import { conflictActions } from '../domain/permissions';
import { scopesOverlap, validateDivergence, type DivergenceError } from '../domain/conflict';
import { addMonths } from '../domain/dates';
import { formatDec, formatPeriod } from '../domain/format';
import { Chip, ErrorBox, Loading, formatAnswerValue, isTypingTarget } from '../components/common';

const REASONS: ReasonCode[] = ['customer_methodology', 'consolidation', 'period', 'product_scope', 'boundary', 'timing_correction'];
const DISMISS: string[] = ['parse_error', 'different_scope', 'rule_bug'];

export function ConflictsPage() {
  const { t } = useI18n();
  const { ds } = useApp();
  const list = useAsync(() => ds.listConflicts(), []);
  const [showClosed, setShowClosed] = useState(false);
  if (list.loading && !list.data) return <Loading />;
  if (list.error) return <ErrorBox error={list.error} />;
  const all = list.data ?? [];
  const open = all.filter((c) => c.conflict.status === 'open').sort((a, b) => (a.conflict.severity === b.conflict.severity ? 0 : a.conflict.severity === 'blocking' ? -1 : 1));
  const closed = all.filter((c) => c.conflict.status !== 'open');
  return (
    <div>
      <div className="page-head">
        <h1>{t('conflicts.title')}</h1>
        <label className="check">
          <input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> {t('conflicts.showClosed')}
        </label>
      </div>
      {open.length === 0 && <div className="empty">{t('conflicts.empty')}</div>}
      <div className="conflict-list" aria-label={t('conflicts.list')}>
        {open.map((v) => (
          <ConflictCard key={v.conflict.id} view={v} />
        ))}
        {showClosed && closed.map((v) => <ConflictCard key={v.conflict.id} view={v} />)}
      </div>
    </div>
  );
}

type Mode = null | 'unify' | 'accept' | 'dismiss';

function ConflictCard({ view }: { view: ConflictView }) {
  const { t, locale } = useI18n();
  const app = useApp();
  const { role, shortcuts } = app;
  const c = view.conflict;
  const sides = view.sides;
  const acts = conflictActions(role);
  const [mode, setMode] = useState<Mode>(null);
  const [base, setBase] = useState(0);
  const [note, setNote] = useState('');
  const [reason, setReason] = useState<ReasonCode>('consolidation');
  const [reqRefs, setReqRefs] = useState<string[]>([]);
  const [basis, setBasis] = useState<string[]>(sides.map((s) => s.answer.consolidation ?? ''));
  const [discDe, setDiscDe] = useState('');
  const [discEn, setDiscEn] = useState('');
  const [includeExport, setIncludeExport] = useState(true);
  const earliestEnd = sides.map((s) => s.answer.period.end).sort()[0] ?? app.ds.today();
  const [reviewBy, setReviewBy] = useState(addMonths(earliestEnd, 12));
  const [dismissReason, setDismissReason] = useState(DISMISS[0]);
  const [errors, setErrors] = useState<DivergenceError[]>([]);
  const [focused, setFocused] = useState(false);
  const isOpen = c.status === 'open';
  const blocking = c.severity === 'blocking';

  const overlap = sides.length >= 2 ? scopesOverlap(sides[0].answer.customer_scope, sides[1].answer.customer_scope) : [];
  const overlapNames = overlap.map((id) => (id === 'all' ? t('answer.all') : sides.find((s) => s.answer.customer_scope !== 'all' && s.answer.customer_scope.includes(id))?.customer_name ?? id)).join(', ');
  const canAccept = acts.accept && sides.length >= 2 && overlap.length === 0;
  const canDismiss = blocking ? acts.dismiss : acts.unify;

  const rows: { key: MessageKey; get: (i: number) => string }[] = [
    { key: 'conflicts.row.customer', get: (i) => sides[i].customer_name },
    { key: 'conflicts.row.value', get: (i) => formatAnswerValue(sides[i].answer, locale, t) },
    { key: 'conflicts.row.method', get: (i) => sides[i].methodology_version ?? '—' },
    { key: 'conflicts.row.period', get: (i) => formatPeriod(sides[i].answer.period, locale) },
    { key: 'conflicts.row.boundary', get: (i) => sides[i].answer.boundary },
    { key: 'conflicts.row.consolidation', get: (i) => sides[i].answer.consolidation ?? '—' },
    { key: 'conflicts.row.status', get: (i) => t(`status.answer.${sides[i].answer.status}` as MessageKey) },
    { key: 'conflicts.row.evidence', get: (i) => `${sides[i].evidence_count} ▸` },
  ];

  const submit = async () => {
    let divergence: Divergence | undefined;
    if (mode === 'accept') {
      divergence = {
        format: 'ADR/1',
        reason_code: reason,
        customer_requirement_refs: reqRefs,
        sides: sides.map((s, i) => ({ answer: `${s.answer.id}#v${s.answer.version}`, customer_scope: s.answer.customer_scope, basis: basis[i] ?? '' })),
        disclosure_text: { ...(discDe.trim() ? { de: discDe.trim() } : {}), ...(discEn.trim() ? { en: discEn.trim() } : {}) },
        include_in_export: includeExport,
        review_by: reviewBy,
      };
      const errs = validateDivergence(divergence, note, sides.map((s) => s.answer.period));
      setErrors(errs);
      if (errs.length) return;
    } else if (note.trim().length < (mode === 'unify' ? 1 : 1)) {
      setErrors(['note_too_short']);
      return;
    }
    try {
      await app.ds.resolveConflict(
        { conflict_id: c.id, kind: mode === 'unify' ? 'corrected' : mode === 'accept' ? 'accepted_divergence' : 'dismissed', note: note.trim(), base_side: mode === 'unify' ? base : undefined, dismiss_reason: mode === 'dismiss' ? dismissReason : undefined, divergence },
        app.actor,
      );
      app.notify(t(mode === 'unify' ? 'conflicts.done.resolved' : mode === 'accept' ? 'conflicts.done.accepted' : 'conflicts.done.dismissed'), 'success');
      app.refresh();
    } catch (e) {
      app.notify(app.errorText(e), 'error');
    }
  };

  useEffect(() => {
    if (!shortcuts || !focused || !isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'ArrowLeft') setBase(0);
      else if (e.key === 'ArrowRight') setBase(Math.min(1, sides.length - 1));
      else if ((e.key === 'u' || e.key === 'U') && acts.unify) setMode('unify');
      else if ((e.key === 'd' || e.key === 'D') && canAccept) setMode('accept');
      else if ((e.key === 'x' || e.key === 'X') && canDismiss) setMode('dismiss');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const questions = useMemo(() => view.questions, [view.questions]);
  const headId = `cfl-${c.id}`;
  return (
    <article className={`conflict ${blocking ? 'conflict-blocking' : 'conflict-warning'} ${isOpen ? '' : 'conflict-closed'}`} aria-labelledby={headId} onFocus={() => setFocused(true)} onBlur={(e) => !e.currentTarget.contains(e.relatedTarget as Node) && setFocused(false)} tabIndex={-1}>
      <header className="conflict-head">
        <h2 id={headId} className="h3">
          <span aria-hidden="true">{blocking ? '⛔ ' : '⚠ '}</span>
          {t('conflicts.heading', { rule: c.rule_id, severity: t(`conflicts.severity.${c.severity}` as MessageKey) })}
        </h2>
        <span className="mono small muted">{c.datapoint_id}</span>
        <Chip tone={isOpen ? (blocking ? 'danger' : 'warn') : 'ok'}>{t(`status.conflict.${c.status}` as MessageKey)}</Chip>
        {c.detail.delta && <span className="small">{t('conflicts.delta', { d: formatDec(c.detail.delta, locale) })}</span>}
      </header>
      {sides.length > 0 && (
        <table className="compare">
          <thead>
            <tr>
              <td />
              {sides.map((s, i) => (
                <th key={s.answer.id} scope="col" className={mode === 'unify' && base === i ? 'base-col' : ''}>
                  {t('conflicts.side', { n: i + 1 })}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const vals = sides.map((_, i) => r.get(i));
              const differs = vals.length > 1 && new Set(vals).size > 1 && r.key !== 'conflicts.row.status' && r.key !== 'conflicts.row.evidence' && r.key !== 'conflicts.row.customer';
              return (
                <tr key={r.key} className={differs ? 'differs' : ''}>
                  <th scope="row">{t(r.key)}</th>
                  {vals.map((v, i) => (
                    <td key={i} className={mode === 'unify' && base === i ? 'base-col' : ''}>
                      {differs ? <strong>{v}</strong> : v}
                      {differs && i === vals.length - 1 && (
                        <span className="diff-mark" title={t('conflicts.diff')}>
                          {' '}
                          ≠<span className="sr-only"> {t('conflicts.diff')}</span>
                        </span>
                      )}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {c.explanation && (
        <p className="ai-note">
          <Chip tone="ai">{t('conflicts.explanation')}</Chip> {pickText(c.explanation, locale)}
        </p>
      )}
      {!isOpen && c.resolution && (
        <p className="small muted">{t('conflicts.resolution', { kind: c.resolution.kind, note: c.resolution.note })}</p>
      )}
      {isOpen && (
        <div className="conflict-actions">
          <button type="button" className={`btn ${mode === 'unify' ? 'btn-primary' : ''}`} onClick={() => setMode('unify')} disabled={!acts.unify || sides.length < 1}>
            {t('conflicts.unify')} <kbd>U</kbd>
          </button>
          <span className="btn-with-hint">
            <button type="button" className={`btn ${mode === 'accept' ? 'btn-primary' : ''}`} onClick={() => setMode('accept')} disabled={!canAccept} aria-describedby={`${headId}-acc`}>
              {t('conflicts.accept')} <kbd>D</kbd>
            </button>
            {!canAccept && (
              <span id={`${headId}-acc`} className="small hint-danger">
                {overlap.length > 0 ? t('conflicts.overlap', { names: overlapNames }) : t('conflicts.ownerOnly')}
              </span>
            )}
          </span>
          <button type="button" className={`btn ${mode === 'dismiss' ? 'btn-primary' : ''}`} onClick={() => setMode('dismiss')} disabled={!canDismiss} title={!canDismiss ? t('conflicts.ownerOnly') : undefined}>
            {t('conflicts.dismiss')} <kbd>X</kbd>
          </button>
        </div>
      )}
      {isOpen && mode && (
        <form
          className="conflict-form"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          {mode === 'unify' && (
            <fieldset>
              <legend>{t('conflicts.baseSide')} (←/→)</legend>
              {sides.map((s, i) => (
                <label key={s.answer.id} className="check">
                  <input type="radio" name={`base-${c.id}`} checked={base === i} onChange={() => setBase(i)} /> {t('conflicts.side', { n: i + 1 })}: {s.customer_name} · {formatAnswerValue(s.answer, locale, t)}
                </label>
              ))}
            </fieldset>
          )}
          {mode === 'accept' && (
            <>
              <label className="field">
                <span>{t('conflicts.reasonCode')}</span>
                <select value={reason} onChange={(e) => setReason(e.target.value as ReasonCode)}>
                  {REASONS.map((r) => (
                    <option key={r} value={r}>
                      {t(`conflicts.reason.${r}` as MessageKey)}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset>
                <legend>{t('conflicts.requirementRefs')}</legend>
                {questions.map((q) => (
                  <label key={q.id} className="check">
                    <input type="checkbox" checked={reqRefs.includes(q.id)} onChange={(e) => setReqRefs((s) => (e.target.checked ? [...s, q.id] : s.filter((x) => x !== q.id)))} /> <span className="mono">{q.native_id}</span> {pickText(q.text, locale)}
                  </label>
                ))}
              </fieldset>
              <div className="grid2">
                {sides.map((s, i) => (
                  <label key={s.answer.id} className="field">
                    <span>
                      {t('conflicts.basis', { n: i + 1 })} ({s.customer_name})
                    </span>
                    <input value={basis[i] ?? ''} onChange={(e) => setBasis((b) => b.map((x, j) => (j === i ? e.target.value : x)))} />
                  </label>
                ))}
              </div>
              <div className="grid2">
                <label className="field">
                  <span>{t('conflicts.disclosureDe')}</span>
                  <textarea rows={2} value={discDe} onChange={(e) => setDiscDe(e.target.value)} />
                </label>
                <label className="field">
                  <span>{t('conflicts.disclosureEn')}</span>
                  <textarea rows={2} value={discEn} onChange={(e) => setDiscEn(e.target.value)} />
                </label>
              </div>
              <div className="grid2">
                <label className="check">
                  <input type="checkbox" checked={includeExport} onChange={(e) => setIncludeExport(e.target.checked)} /> {t('conflicts.includeInExport')}
                </label>
                <label className="field">
                  <span>{t('conflicts.reviewBy')}</span>
                  <input type="date" value={reviewBy} max={addMonths(earliestEnd, 15)} onChange={(e) => setReviewBy(e.target.value)} />
                </label>
              </div>
            </>
          )}
          {mode === 'dismiss' && (
            <label className="field">
              <span>{t('conflicts.dismissReason')}</span>
              <select value={dismissReason} onChange={(e) => setDismissReason(e.target.value)}>
                {DISMISS.map((d) => (
                  <option key={d} value={d}>
                    {t(`conflicts.dismissReason.${d}` as MessageKey)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="field">
            <span>
              {t('conflicts.note')} {mode === 'accept' && <span className="muted small">{t('conflicts.noteMin', { n: note.trim().length })}</span>}
            </span>
            <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} required aria-required="true" aria-invalid={errors.includes('note_too_short')} />
          </label>
          {errors.length > 0 && (
            <ul className="banner banner-danger" role="alert" aria-live="polite">
              {errors.map((er) => (
                <li key={er}>{t(`conflicts.err.${er}` as MessageKey)}</li>
              ))}
            </ul>
          )}
          <div className="conflict-actions">
            <button type="submit" className="btn btn-primary">
              {t('common.confirm')}
            </button>
            <button type="button" className="btn" onClick={() => (setMode(null), setErrors([]))}>
              {t('common.cancel')}
            </button>
          </div>
        </form>
      )}
    </article>
  );
}
