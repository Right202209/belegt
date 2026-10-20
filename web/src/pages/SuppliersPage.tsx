// Spec: 01 §2.3 次要旅程 B（CBAM 上游追数）、§3.5（供应商状态与成本差列）；03 §3.10 SupplierRequest；
//       08 §4.1 chase-policy@1.0.0；06 §4.4 non_answer_subjects.supplier_request_first_send；13（CBAM 日期）
import { Fragment, useState } from 'react';
import type { SupplierRequestView } from '../types';
import { useApp, useAsync } from '../state/store';
import { useI18n, type MessageKey } from '../i18n';
import { daysBetween } from '../domain/dates';
import { formatDate, formatDateTime, formatEur } from '../domain/format';
import { supplierActions } from '../domain/permissions';
import { Chip, ErrorBox, Loading } from '../components/common';

const TONE: Record<string, 'ok' | 'warn' | 'danger' | 'info' | 'neutral'> = {
  draft: 'neutral',
  sent: 'info',
  reminded: 'warn',
  responded: 'info',
  reviewed: 'ok',
  closed: 'ok',
  escalated: 'danger',
  expired: 'danger',
};

export function SuppliersPage() {
  const { t, locale } = useI18n();
  const app = useApp();
  const list = useAsync(() => app.ds.listSupplierRequests(), []);
  const [open, setOpen] = useState<string | null>(null);
  if (list.loading && !list.data) return <Loading />;
  if (list.error) return <ErrorBox error={list.error} />;
  const views = list.data ?? [];
  const acts = supplierActions(app.role);
  const total = views.filter((v) => !['closed', 'reviewed'].includes(v.supplier_request.status)).reduce((s, v) => s + Number(v.ui_cost_gap_eur ?? 0), 0);

  const approve = async (v: SupplierRequestView) => {
    try {
      await app.ds.approveSupplierFirstSend(v.supplier_request.id, app.actor);
      app.notify(t('suppliers.sent'), 'success');
      app.refresh();
    } catch (e) {
      app.notify(app.errorText(e), 'error');
    }
  };

  return (
    <div>
      <div className="page-head">
        <h1>{t('suppliers.title')}</h1>
      </div>
      <div className="kpis">
        <p className="kpi">
          <strong>{t('suppliers.totalGap', { sum: formatEur(String(total), locale) })}</strong> <Chip tone="warn">{t('inbox.estimate')}</Chip>
        </p>
        <p className="muted small">{t('suppliers.cbamDeadline', { decl: formatDate('2027-09-30', locale), internal: formatDate('2027-06-30', locale) })}</p>
        <p className="muted small">{t('suppliers.policy')}</p>
      </div>
      {views.length === 0 ? (
        <div className="empty">{t('suppliers.empty')}</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t('suppliers.col.supplier')}</th>
              <th scope="col">{t('suppliers.col.status')}</th>
              <th scope="col">{t('suppliers.col.reminders')}</th>
              <th scope="col">{t('suppliers.col.lastSent')}</th>
              <th scope="col">{t('suppliers.col.due')}</th>
              <th scope="col" className="num">
                {t('suppliers.col.gap')}
              </th>
              <th scope="col">
                <span className="sr-only">{t('suppliers.timeline')}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {views.map((v) => {
              const sr = v.supplier_request;
              const sent = v.history.find((h) => h.status === 'sent');
              const dPlus = sent ? daysBetween(sent.created_at.slice(0, 10), app.ds.today()) : null;
              const isOpen = open === sr.id;
              return (
                <Fragment key={sr.id}>
                  <tr>
                    <th scope="row">
                      {v.supplier_name}
                      <div className="muted small mono">{sr.template_id}</div>
                    </th>
                    <td>
                      <Chip tone={TONE[sr.status]}>{t(`status.sreq.${sr.status}` as MessageKey)}</Chip>{' '}
                      {dPlus !== null && !['closed', 'expired'].includes(sr.status) && <span className="small muted">{t('suppliers.daysSince', { n: dPlus })}</span>}
                    </td>
                    <td className="num">{sr.reminder_count}</td>
                    <td>{sr.last_sent_at ? formatDate(sr.last_sent_at, locale) : '—'}</td>
                    <td>{formatDate(sr.due_date, locale)}</td>
                    <td className="num">
                      {v.ui_cost_gap_eur ? formatEur(v.ui_cost_gap_eur, locale) : '—'} {sr.status === 'closed' && <span aria-label="✓">✓</span>}
                    </td>
                    <td className="actions-cell">
                      <button type="button" className="btn btn-small" aria-expanded={isOpen} aria-controls={`tl-${sr.id}`} onClick={() => setOpen(isOpen ? null : sr.id)}>
                        {t('suppliers.timeline')}
                      </button>
                      {sr.status === 'draft' && (
                        <button type="button" className="btn btn-small btn-primary" disabled={!acts.approveFirstSend} title={!acts.approveFirstSend ? t('common.requiredRole', { roles: t('role.compliance_owner') }) : undefined} onClick={() => approve(v)}>
                          {t('suppliers.approveSend')}
                        </button>
                      )}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr id={`tl-${sr.id}`} className="timeline-row">
                      <td colSpan={7}>
                        <div className="tl-meta small">
                          {t('suppliers.language')}: <span className="mono">{sr.language}</span> · {t('suppliers.datapoints')}: <span className="mono">{sr.datapoint_ids.map((d) => d.split('@')[0]).join(', ')}</span>
                        </div>
                        <ol className="timeline">
                          {v.history.map((h) => (
                            <li key={h.version} className={`tl-${h.status}`}>
                              <span className="tl-dot" aria-hidden="true" />
                              <span className="tl-date">{formatDateTime(h.created_at, locale)}</span>
                              <Chip tone={TONE[h.status]}>{t(`status.sreq.${h.status}` as MessageKey)}</Chip>
                              <span className="mono small muted">
                                v{h.version} · {h.created_by}
                              </span>
                              {h.status === 'reminded' && <span className="small"> #{h.reminder_count}</span>}
                            </li>
                          ))}
                        </ol>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
