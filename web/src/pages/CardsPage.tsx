// Spec: 01 §3.2 UI-02 封闭式问题卡片（yes_no / single_choice Top-3 / numeric_confirm / file_request）；03 §3.11；AC-104-1…3
import { useEffect, useMemo, useState } from 'react';
import type { ClosedQuestion } from '../types';
import { useApp, useAsync } from '../state/store';
import { pickText, useI18n, type MessageKey } from '../i18n';
import { canAnswerCard } from '../domain/permissions';
import { formatDateTime, formatDec, parseLocalizedNumber } from '../domain/format';
import { Chip, ErrorBox, Loading, isTypingTarget } from '../components/common';
import { ApiException } from '../api/types';

const KIND_ICON: Record<ClosedQuestion['kind'], string> = { numeric_confirm: '◆', single_choice: '☰', yes_no: '?', file_request: '⇪' };

export function CardsPage() {
  const { t, locale } = useI18n();
  const app = useApp();
  const { ds, role, actor, shortcuts } = app;
  const cards = useAsync(() => ds.listClosedQuestions(), []);
  const [idx, setIdx] = useState(0);
  const [done, setDone] = useState<Set<string>>(new Set());
  const [choice, setChoice] = useState<string>('');
  const [correcting, setCorrecting] = useState(false);
  const [corrected, setCorrected] = useState('');
  const [ambiguity, setAmbiguity] = useState<[string, string] | null>(null);
  const [upload, setUpload] = useState<{ sha: string; name: string }[]>([]);
  const [error, setError] = useState<string | null>(null);

  const open = useMemo(() => (cards.data ?? []).filter((c) => c.status === 'open' && !done.has(c.id) && canAnswerCard(role, c.assignee_role)), [cards.data, done, role]);
  const expired = useMemo(() => (cards.data ?? []).filter((c) => c.status === 'expired' && canAnswerCard(role, c.assignee_role)), [cards.data, role]);
  const card = open[Math.min(idx, Math.max(0, open.length - 1))];

  const reset = () => {
    setChoice('');
    setCorrecting(false);
    setCorrected('');
    setAmbiguity(null);
    setUpload([]);
    setError(null);
  };
  useEffect(reset, [card?.id]);

  const submit = async (payload: { choice?: string; bool?: boolean; corrected_value?: string; blob_sha256?: string[] }) => {
    if (!card) return;
    try {
      await ds.answerClosedQuestion({ cq_id: card.id, ...payload }, actor);
      setDone((s) => new Set(s).add(card.id));
      app.notify(t('cards.answered'), 'success');
      app.refresh();
    } catch (e) {
      if (e instanceof ApiException && e.error.code === 'GRAPH_ILLEGAL_TRANSITION') setError(t('cards.alreadyAnswered'));
      else setError(app.errorText(e));
    }
  };

  const submitCorrection = () => {
    const p = parseLocalizedNumber(corrected, locale);
    if (p.ambiguous) {
      setAmbiguity(p.ambiguous);
      return;
    }
    if (!p.value) {
      setError(t('cards.invalidNumber', { example: locale === 'de' ? '1.284.300,5' : '1,284,300.5' }));
      return;
    }
    submit({ corrected_value: p.value });
  };

  const skip = () => setIdx((i) => (open.length ? (i + 1) % open.length : 0));
  const forward = () => {
    app.notify(t('cards.forwarded'), 'info');
    skip();
  };

  useEffect(() => {
    if (!shortcuts || !card) return;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e) || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (card.kind === 'yes_no' && (k === 'y' || k === 'j')) submit({ bool: true });
      else if (card.kind === 'yes_no' && k === 'n') submit({ bool: false });
      else if (card.kind === 'single_choice' && /^[1-4]$/.test(k)) {
        const opt = card.options?.[Number(k) - 1];
        if (opt) setChoice(opt.key);
      } else if (k === 'enter') {
        if (card.kind === 'numeric_confirm' && !correcting) submit({});
        else if (card.kind === 'single_choice' && choice) submit({ choice });
      } else if (k === 'c' && card.kind === 'numeric_confirm') setCorrecting(true);
      else if (k === 's') skip();
      else if (k === 'f') forward();
      else if (k === 'arrowright') setIdx((i) => Math.min(open.length - 1, i + 1));
      else if (k === 'arrowleft') setIdx((i) => Math.max(0, i - 1));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (cards.loading && !cards.data) return <Loading />;
  if (cards.error) return <ErrorBox error={cards.error} />;

  const minutes = Math.max(1, Math.ceil(open.length * 0.5));
  return (
    <div className="cards">
      <div className="page-head">
        <h1>{t('cards.title')}</h1>
        <p className="counter" aria-live="polite">
          {t('cards.counter', { n: open.length, min: minutes })}
        </p>
      </div>
      {shortcuts && <p className="muted small">{t('cards.keys')}</p>}
      {!card ? (
        <div className="empty">{t('cards.empty')}</div>
      ) : (
        <article className="card" aria-labelledby="card-prompt" aria-roledescription="card">
          <header className="card-head">
            <span>
              <span aria-hidden="true">{KIND_ICON[card.kind]}</span> {t(`cards.kind.${card.kind}` as MessageKey)} · {t('cards.position', { i: Math.min(idx, open.length - 1) + 1, n: open.length })}
            </span>
            <span className="muted small">{t('cards.expires', { date: formatDateTime(card.expires_at, locale) })}</span>
          </header>
          <h2 id="card-prompt" className="card-prompt">
            {pickText(card.prompt, locale)}
          </h2>
          <p className="muted small">
            {t('cards.assignee', { role: t(`role.${card.assignee_role}` as MessageKey) })} · <span className="mono">{card.created_by}</span>
          </p>

          {card.kind === 'numeric_confirm' && card.proposed_value && (
            <div className="card-body">
              <div className="context-box">
                <span className="muted small">{t('lineage.excerpt')}</span>
                <p className="big-value">
                  {formatDec(card.proposed_value.value, locale)} {card.proposed_value.unit}
                </p>
                <span className="mono small muted">{card.proposed_value.evidence_ref}</span>
              </div>
              {!correcting ? (
                <div className="card-actions">
                  <button type="button" className="btn btn-primary btn-lg" onClick={() => submit({})}>
                    ✓ {t('cards.correct')} <kbd>Enter</kbd>
                  </button>
                  <button type="button" className="btn btn-lg" onClick={() => setCorrecting(true)}>
                    ✎ {t('cards.fix')} <kbd>C</kbd>
                  </button>
                </div>
              ) : (
                <div className="correction">
                  <label className="field">
                    <span>
                      {t('cards.correctedValue')} ({card.proposed_value.unit})
                    </span>
                    <input inputMode="decimal" value={corrected} onChange={(e) => (setCorrected(e.target.value), setAmbiguity(null))} autoFocus />
                  </label>
                  <p className="small muted">{t('cards.correctionNote')}</p>
                  {ambiguity && (
                    <div className="banner banner-warn" role="alert">
                      {t('cards.ambiguous', { a: formatDec(ambiguity[0], locale), b: formatDec(ambiguity[1], locale) })}{' '}
                      <button type="button" className="btn btn-small" onClick={() => submit({ corrected_value: ambiguity[0] })}>
                        {formatDec(ambiguity[0], locale)}
                      </button>{' '}
                      <button type="button" className="btn btn-small" onClick={() => submit({ corrected_value: ambiguity[1] })}>
                        {formatDec(ambiguity[1], locale)}
                      </button>
                    </div>
                  )}
                  <div className="card-actions">
                    <button type="button" className="btn btn-primary" onClick={submitCorrection} disabled={!corrected.trim()}>
                      {t('cards.submit')}
                    </button>
                    <button type="button" className="btn" onClick={() => setCorrecting(false)}>
                      {t('common.cancel')}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {card.kind === 'yes_no' && (
            <div className="card-actions">
              <button type="button" className="btn btn-primary btn-lg" onClick={() => submit({ bool: true })}>
                {t('common.yes')} <kbd>Y</kbd>
              </button>
              <button type="button" className="btn btn-lg" onClick={() => submit({ bool: false })}>
                {t('common.no')} <kbd>N</kbd>
              </button>
            </div>
          )}

          {card.kind === 'single_choice' && (
            <fieldset className="card-body">
              <legend className="sr-only">{pickText(card.prompt, locale)}</legend>
              {(card.options ?? []).slice(0, 4).map((o, i) => (
                <label key={o.key} className={`option ${choice === o.key ? 'option-selected' : ''}`}>
                  <input type="radio" name={`cq-${card.id}`} value={o.key} checked={choice === o.key} onChange={() => setChoice(o.key)} />
                  <kbd>{i + 1}</kbd> <strong>{o.key})</strong> {pickText(o.label, locale)}
                </label>
              ))}
              <div className="card-actions">
                <button type="button" className="btn btn-primary btn-lg" disabled={!choice} onClick={() => submit({ choice })}>
                  {t('cards.submit')} <kbd>Enter</kbd>
                </button>
              </div>
            </fieldset>
          )}

          {card.kind === 'file_request' && (
            <div className="card-body">
              <label className="field">
                <span>{t('cards.upload')}</span>
                <input
                  type="file"
                  multiple
                  accept=".pdf,.png,.jpg,.jpeg,.xlsx"
                  onChange={async (e) => {
                    const files = Array.from(e.target.files ?? []);
                    const shas = await Promise.all(files.map(async (f) => ({ sha: await ds.uploadBlob(f), name: f.name })));
                    setUpload(shas);
                  }}
                />
              </label>
              {upload.map((u) => (
                <p key={u.sha} className="small">
                  {t('cards.uploaded', { name: u.name })} <span className="mono muted">{u.sha.slice(0, 23)}…</span>
                </p>
              ))}
              <div className="card-actions">
                <button type="button" className="btn btn-primary btn-lg" disabled={!upload.length} onClick={() => submit({ blob_sha256: upload.map((u) => u.sha) })}>
                  {t('cards.submit')}
                </button>
                <button type="button" className="btn btn-lg" onClick={() => submit({ bool: false })}>
                  {t('cards.notAvailable')}
                </button>
              </div>
            </div>
          )}

          {error && (
            <p className="banner banner-danger" role="alert">
              {error}
            </p>
          )}

          <footer className="card-foot">
            <button type="button" className="btn btn-link" onClick={() => setIdx((i) => Math.max(0, i - 1))} disabled={idx === 0}>
              ← {t('common.prev')}
            </button>
            <span>
              <button type="button" className="btn btn-link" onClick={forward}>
                {t('cards.dontKnow')} · {t('cards.forward')} <kbd>F</kbd>
              </button>
              <button type="button" className="btn btn-link" onClick={skip}>
                {t('cards.skip')} <kbd>S</kbd>
              </button>
            </span>
            <button type="button" className="btn btn-link" onClick={() => setIdx((i) => Math.min(open.length - 1, i + 1))} disabled={idx >= open.length - 1}>
              {t('common.next')} →
            </button>
          </footer>
        </article>
      )}

      {expired.length > 0 && (
        <section className="expired-cards">
          <h2 className="h3">{t('cards.expiredList')}</h2>
          <ul>
            {expired.map((c) => (
              <li key={c.id} className="muted">
                <Chip tone="neutral">{t('status.cq.expired')}</Chip> {pickText(c.prompt, locale)} · {t('cards.expiredNote')}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
