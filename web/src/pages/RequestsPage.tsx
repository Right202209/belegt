// Spec: 01 §3.4 UI-04 问卷详情与进度、§2.1 关键体验承诺（下一步 + 还差多少；侧路状态用户语言 + 唯一主按钮）；
//       _baseline §E Request.status 流水线；03 §7 审计包导出
import { useMemo, useState } from 'react';
import type { RequestView } from '../types';
import { useApp, useAsync } from '../state/store';
import { pickText, useI18n, type MessageKey } from '../i18n';
import { PIPELINE, parseStatusReason, pipelineIndex } from '../domain/requestStatus';
import { daysBetween } from '../domain/dates';
import { formatDate } from '../domain/format';
import { canExportAuditPack, canSeeDraftValues } from '../domain/permissions';
import { Chip, ErrorBox, Loading, ProgressBar, RiskBadge, formatAnswerValue, truncate } from '../components/common';
import { EvidencePanel } from '../components/EvidencePanel';
import type { QuestionRow } from '../api/types';

function statusTone(s: string): 'ok' | 'warn' | 'danger' | 'info' | 'neutral' {
  if (s === 'halted' || s === 'cancelled') return 'danger';
  if (s === 'awaiting_human' || s === 'awaiting_supplier') return 'warn';
  if (s === 'submitted' || s === 'archived' || s === 'approved') return 'ok';
  return 'info';
}

function DueLabel({ due, today }: { due?: string; today: string }) {
  const { t, locale } = useI18n();
  if (!due) return <>—</>;
  const d = daysBetween(today, due);
  return (
    <span>
      {formatDate(due, locale)} <span className={`muted small ${d < 14 && d >= 0 ? 'due-soon' : ''}`}>({d >= 0 ? t('requests.daysLeft', { n: d }) : t('requests.overdue', { n: -d })})</span>
    </span>
  );
}

export function RequestsPage() {
  const { t } = useI18n();
  const { ds, route, navigate } = useApp();
  const list = useAsync(() => ds.listRequests(), []);
  if (list.loading && !list.data) return <Loading />;
  if (list.error) return <ErrorBox error={list.error} />;
  const views = list.data ?? [];
  const selected = route.param ? views.find((v) => v.request.id === route.param) : undefined;
  if (selected) return <RequestDetail view={selected} onBack={() => navigate('requests')} />;
  const done = (v: RequestView) => ['submitted', 'archived', 'cancelled'].includes(v.request.status);
  const sorted = [...views].sort((a, b) => Number(done(a)) - Number(done(b)) || ((a.request.due_date ?? '9999') < (b.request.due_date ?? '9999') ? -1 : 1));
  return (
    <div>
      <div className="page-head">
        <h1>{t('requests.title')}</h1>
      </div>
      <table className="table">
        <caption className="sr-only">{t('requests.title')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('requests.col.title')}</th>
            <th scope="col">{t('requests.col.customer')}</th>
            <th scope="col">{t('requests.col.due')}</th>
            <th scope="col">{t('requests.col.status')}</th>
            <th scope="col">{t('requests.col.progress')}</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((v) => (
            <RequestRow key={v.request.id} v={v} today={ds.today()} onOpen={() => navigate('requests', v.request.id)} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RequestRow({ v, today, onOpen }: { v: RequestView; today: string; onOpen: () => void }) {
  const { t } = useI18n();
  const p = v.progress;
  const label = t('requests.progressSummary', { total: p.questions, mapped: p.mapped, evidenced: p.evidenced, approved: p.approved, submitted: p.submitted });
  return (
    <tr>
      <th scope="row">
        <button type="button" className="btn-link link-strong" onClick={onOpen}>
          {v.request.title}
        </button>
        {v.request.template_ref && <div className="muted small mono">{v.request.template_ref}</div>}
      </th>
      <td>{v.counterparty_name}</td>
      <td>
        <DueLabel due={v.request.due_date} today={today} />
      </td>
      <td>
        <Chip tone={statusTone(v.request.status)}>{t(`status.request.${v.request.status}` as MessageKey)}</Chip>
      </td>
      <td className="progress-cell">
        {p.questions > 0 ? (
          <>
            <ProgressBar total={p.questions} mapped={p.mapped} evidenced={p.evidenced} approved={p.approved} submitted={p.submitted} label={label} />
            <span className="small muted">
              {p.approved}/{p.questions}
            </span>
          </>
        ) : (
          <span className="small muted">{t('requests.emptyReceived')}</span>
        )}
      </td>
    </tr>
  );
}

function RequestDetail({ view, onBack }: { view: RequestView; onBack: () => void }) {
  const { t, locale } = useI18n();
  const app = useApp();
  const { ds, role, navigate } = app;
  const r = view.request;
  const p = view.progress;
  const rows = useAsync(() => ds.listQuestions(r.id), [r.id]);
  const [openRow, setOpenRow] = useState<QuestionRow | null>(null);
  const reason = parseStatusReason(r.status_reason);
  const idx = pipelineIndex(r.status, r.status_reason);
  const pendingApprovals = useMemo(() => (rows.data ?? []).filter((x) => x.answer?.status === 'in_review').length, [rows.data]);
  const showValues = canSeeDraftValues(role);

  const next = p.open_conflicts > 0 ? { label: t('requests.next.conflicts', { n: p.open_conflicts }), go: () => navigate('conflicts') } : pendingApprovals > 0 && (role === 'compliance_owner' || role === 'executive') ? { label: t('requests.next.inbox', { n: pendingApprovals }), go: () => navigate('inbox') } : p.open_cards > 0 ? { label: t('requests.next.cards', { n: p.open_cards }), go: () => navigate('cards') } : null;

  const exportPack = async () => {
    try {
      const { filename, blob } = await ds.exportAuditPack(r.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      app.notify(t('requests.auditPackDone', { file: filename }), 'success');
    } catch (e) {
      app.notify(app.errorText(e), 'error');
    }
  };

  const summary = t('requests.progressSummary', { total: p.questions, mapped: p.mapped, evidenced: p.evidenced, approved: p.approved, submitted: p.submitted });
  return (
    <div className="request-detail">
      <button type="button" className="btn btn-link" onClick={onBack}>
        ← {t('requests.back')}
      </button>
      <header className="detail-head">
        <h1>{r.title}</h1>
        <p className="muted">
          {view.counterparty_name} · {t('requests.col.due')}: <DueLabel due={r.due_date} today={ds.today()} /> · {t('requests.template')}: <span className="mono">{r.template_ref ?? '—'}</span> · {t('requests.assignee')}: <span className="mono">{r.assignee ?? '—'}</span> ·{' '}
          <Chip tone={statusTone(r.status)}>
            <span aria-hidden="true">◉ </span>
            {t(`status.request.${r.status}` as MessageKey)}
          </Chip>
        </p>
      </header>

      {r.status === 'halted' && (
        <div className="banner banner-danger" role="alert">
          {t('requests.halted')} <span className="mono small">({r.status_reason})</span>{' '}
          <button type="button" className="btn btn-small" disabled={role !== 'compliance_owner'} title={role !== 'compliance_owner' ? t('common.requiredRole', { roles: t('role.compliance_owner') }) : undefined}>
            {t('requests.resume')}
          </button>
        </div>
      )}
      {(r.status === 'awaiting_human' || r.status === 'awaiting_supplier') && (
        <div className="banner banner-warn" role="status">
          {t(`requests.banner.${r.status}` as MessageKey)}
          {reason.return_to && <span className="muted small"> (return_to: {reason.return_to})</span>}
        </div>
      )}

      <section aria-label={t('requests.pipeline')}>
        <h2 className="h3">{t('requests.pipeline')}</h2>
        <ol className="pipeline">
          {PIPELINE.map((s, i) => (
            <li key={s} className={i < idx ? 'done' : i === idx ? 'current' : ''} aria-current={i === idx ? 'step' : undefined}>
              <span className="dot" aria-hidden="true">
                {i < idx ? '✓' : i === idx ? '●' : '○'}
              </span>
              <span className="pl-label">{t(`status.request.${s}` as MessageKey)}</span>
            </li>
          ))}
        </ol>
      </section>

      {p.questions > 0 ? (
        <section className="progress-box">
          <ProgressBar total={p.questions} mapped={p.mapped} evidenced={p.evidenced} approved={p.approved} submitted={p.submitted} label={summary} />
          <p>{summary}</p>
          <ul className="legend" aria-hidden="true">
            <li>
              <span className="sw seg-mapped" /> {t('requests.legend.mapped')}
            </li>
            <li>
              <span className="sw seg-evidenced" /> {t('requests.legend.evidenced')}
            </li>
            <li>
              <span className="sw seg-approved" /> {t('requests.legend.approved')}
            </li>
            <li>
              <span className="sw seg-submitted" /> {t('requests.legend.submitted')}
            </li>
          </ul>
          <div className="next-row">
            <span>{t('requests.remaining', { n: p.est_minutes_remaining })}</span>
            {next ? (
              <button type="button" className="btn btn-primary" onClick={next.go}>
                {next.label} ▸
              </button>
            ) : (
              <span className="muted">{t('requests.next.none')}</span>
            )}
            {canExportAuditPack(role) && (
              <button type="button" className="btn" onClick={exportPack}>
                ⤓ {t('requests.auditPack')}
              </button>
            )}
          </div>
        </section>
      ) : (
        <div className="empty">{t('requests.emptyReceived')}</div>
      )}

      <section>
        <h2 className="h3">{t('requests.questions')}</h2>
        {rows.loading && !rows.data ? (
          <Loading />
        ) : (rows.data ?? []).length === 0 ? (
          <p className="muted">{t('requests.noQuestions')}</p>
        ) : (
          <div className={`split ${openRow?.answer ? 'with-panel' : ''}`}>
            <div className="table-wrap">
              <table className="table table-dense">
                <thead>
                  <tr>
                    <th scope="col">{t('requests.col.native')}</th>
                    <th scope="col">{t('requests.col.question')}</th>
                    <th scope="col">{t('requests.col.datapoint')}</th>
                    <th scope="col">{t('requests.col.answer')}</th>
                    <th scope="col">{t('requests.col.answerStatus')}</th>
                    <th scope="col">{t('requests.col.risk')}</th>
                    <th scope="col">{t('requests.col.evidence')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(rows.data ?? []).map((row) => {
                    const a = row.answer;
                    const hidden = a && !showValues && (a.status === 'draft' || a.status === 'in_review');
                    return (
                      <tr key={row.question.id} className={openRow?.question.id === row.question.id ? 'row-selected' : ''}>
                        <td className="mono">{row.question.native_id}</td>
                        <td>{truncate(pickText(row.question.text, locale), 70)}</td>
                        <td className="mono small">{a ? a.datapoint_id.split('@')[0] : row.question.status === 'parsed' || row.question.status === 'unmapped' ? <Chip tone="warn">{t('requests.unmapped')}</Chip> : '—'}</td>
                        <td>
                          {a ? (
                            hidden ? (
                              <span className="muted">{t('requests.valueHidden')}</span>
                            ) : (
                              <button type="button" className="btn-link" onClick={() => setOpenRow(row)} aria-label={`${t('lineage.open')}: ${row.question.native_id}`}>
                                {truncate(formatAnswerValue(a, locale, t), 28)} ⓘ
                              </button>
                            )
                          ) : (
                            '—'
                          )}
                        </td>
                        <td>{a ? t(`status.answer.${a.status}` as MessageKey) : '—'}</td>
                        <td>{row.risk_level ? <RiskBadge level={row.risk_level} /> : '—'}</td>
                        <td className="num">{row.evidence_count || '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {openRow?.answer && (
              <aside className="inbox-panel">
                <button type="button" className="btn btn-link" onClick={() => setOpenRow(null)}>
                  ✕ {t('common.close')}
                </button>
                <EvidencePanel key={openRow.answer.id} answer={openRow.answer} questionText={`${openRow.question.native_id} · ${pickText(openRow.question.text, locale)}`} />
              </aside>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
