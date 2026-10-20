// Spec: 01 §3.1 UI-01 审批收件箱；06 §4.3 各级审批要求、§5.1 批量安全条件、§5.2 撤销窗口；AC-403-1…3
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { InboxItem, RiskLevel, RequestView } from '../types';
import { useApp, useAsync } from '../state/store';
import { pickText, useI18n, type MessageKey } from '../i18n';
import { evaluateBatch, isStale, type BatchEvaluation } from '../domain/batch';
import { canApprove, requiredRolesFor } from '../domain/policy';
import { inboxScope } from '../domain/permissions';
import { dueSoonerThanWindow, formatCountdown, remainingMs } from '../domain/undo';
import { formatDate } from '../domain/format';
import { Chip, Dialog, ErrorBox, Loading, RiskBadge, ValueQuad, isTypingTarget, truncate } from '../components/common';
import { EvidencePanel } from '../components/EvidencePanel';
import { ApiException } from '../api/types';

const LEVELS: RiskLevel[] = ['L3', 'L2', 'L1', 'L0'];

export function InboxPage() {
  const { t, locale } = useI18n();
  const app = useApp();
  const { ds, role, actor, stagedIds, shortcuts } = app;
  const inbox = useAsync(() => ds.listInbox(), []);
  const reqs = useAsync(() => ds.listRequests(), []);
  const [requestFilter, setRequestFilter] = useState('');
  const [mine, setMine] = useState(false);
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState<Set<RiskLevel>>(new Set(['L3', 'L2']));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [viewed, setViewed] = useState<Set<string>>(new Set());
  const [ack, setAck] = useState<Set<string>>(new Set());
  const [warnNotes, setWarnNotes] = useState<Record<string, string>>({});
  const [batchDlg, setBatchDlg] = useState<BatchEvaluation | null>(null);
  const [rejectDlg, setRejectDlg] = useState<InboxItem | null>(null);
  const [modifyDlg, setModifyDlg] = useState<InboxItem | null>(null);
  const [l3Dlg, setL3Dlg] = useState<InboxItem | null>(null);
  const [rowError, setRowError] = useState<{ id: string; text: string; roleIssue: boolean } | null>(null);
  const [locallyDone, setLocallyDone] = useState<Set<string>>(new Set());
  const searchRef = useRef<HTMLInputElement>(null);

  const scope = inboxScope(role);
  const reqMap = useMemo(() => new Map((reqs.data ?? []).map((r) => [r.request.id, r])), [reqs.data]);
  const cpNames = useMemo(() => new Map((reqs.data ?? []).map((r) => [r.request.counterparty_id, r.counterparty_name])), [reqs.data]);

  const items = useMemo(() => {
    let list = (inbox.data ?? []).filter((it) => !stagedIds.has(it.approval.id) && !locallyDone.has(it.approval.id));
    if (scope === 'l3_and_assigned') list = list.filter((it) => it.approval.risk_level === 'L3' || it.approval.assignee === actor);
    if (requestFilter) list = list.filter((it) => it.approval.request_id === requestFilter);
    if (mine) list = list.filter((it) => it.approval.assignee === actor);
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      list = list.filter((it) => {
        const qt = it.questions.map((x) => `${x.native_id ?? ''} ${pickText(x.text, locale)}`).join(' ');
        return `${qt} ${it.answer.datapoint_id} ${String(it.answer.value)} ${it.request_title}`.toLowerCase().includes(q);
      });
    }
    return list;
  }, [inbox.data, stagedIds, locallyDone, scope, actor, requestFilter, mine, query, locale]);

  const groups = useMemo(() => {
    const g: Record<RiskLevel, InboxItem[]> = { L0: [], L1: [], L2: [], L3: [] };
    items.forEach((it) => g[it.approval.risk_level].push(it));
    return g;
  }, [items]);

  const visibleRows = useMemo(() => LEVELS.flatMap((l) => (expanded.has(l) ? groups[l] : [])), [groups, expanded]);
  const selected = useMemo(() => items.find((it) => it.approval.id === selectedId) ?? null, [items, selectedId]);

  const ctxFor = useCallback(() => ({ role, now: new Date(), viewed, estimateAcked: ack, expandedLevels: expanded }), [role, viewed, ack, expanded]);
  const evaluations = useMemo(() => {
    const ctx = ctxFor();
    return Object.fromEntries(LEVELS.map((l) => [l, evaluateBatch(l, groups[l], ctx)])) as Record<RiskLevel, BatchEvaluation>;
  }, [groups, ctxFor]);

  const select = (it: InboxItem) => {
    setSelectedId(it.approval.id);
    setPanelOpen(true);
    setViewed((v) => (v.has(it.approval.id) ? v : new Set(v).add(it.approval.id)));
    setRowError(null);
  };

  const toggleGroup = (l: RiskLevel) => setExpanded((s) => (s.has(l) ? new Set([...s].filter((x) => x !== l)) : new Set(s).add(l)));

  const markDone = (id: string) => setLocallyDone((s) => new Set(s).add(id));

  const approveSingle = async (it: InboxItem, extra: { clause_read_ack?: boolean; step_up_auth?: boolean } = {}) => {
    const level = it.approval.risk_level;
    if (level === 'L3' && !extra.clause_read_ack) {
      setL3Dlg(it);
      return;
    }
    if (level === 'L2' && it.answer.is_estimate && !ack.has(it.approval.id)) {
      setRowError({ id: it.approval.id, text: t('batch.reason.estimate_ack_required'), roleIssue: false });
      return;
    }
    const hasWarn = it.conflicts.some((c) => c.status === 'open' && c.severity === 'warning');
    if ((level === 'L2' || level === 'L3') && hasWarn && !(warnNotes[it.approval.id] ?? '').trim()) {
      setRowError({ id: it.approval.id, text: t('inbox.warnNote'), roleIssue: false });
      return;
    }
    try {
      await ds.decide(
        {
          approval_id: it.approval.id,
          decision: 'approved',
          comment: warnNotes[it.approval.id] || undefined,
          confirmations: { estimate_ack: ack.has(it.approval.id) || undefined, warn_note: warnNotes[it.approval.id], ...extra },
        },
        actor,
      );
      markDone(it.approval.id);
      app.notify(t('inbox.approved'), 'success');
      moveSelection(it, 1);
      app.refresh();
    } catch (e) {
      setRowError({ id: it.approval.id, text: app.errorText(e), roleIssue: e instanceof ApiException && e.error.code === 'GRAPH_APPROVAL_REQUIRED' });
    }
  };

  const moveSelection = (from: InboxItem | null, delta: number) => {
    if (!visibleRows.length) return;
    const idx = from ? visibleRows.findIndex((r) => r.approval.id === from.approval.id) : -1;
    const next = visibleRows[Math.max(0, Math.min(visibleRows.length - 1, idx + delta))];
    if (next && next.approval.id !== from?.approval.id) select(next);
  };

  const openBatch = (l: RiskLevel) => {
    const ev = evaluations[l];
    if (l === 'L3') {
      app.notify(t('inbox.l3Hint'), 'info');
      return;
    }
    if (ev.showButton) setBatchDlg(ev);
    else if (ev.blockers.length) app.notify(t(`batch.blocker.${ev.blockers[0]}` as MessageKey), 'info');
  };

  // 快捷键（01 §3.1）：可在顶部关闭（WCAG 2.1.4）
  useEffect(() => {
    if (!shortcuts) return;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e) || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key;
      if (k === 'j' || k === 'J') {
        if (e.shiftKey) return;
        e.preventDefault();
        moveSelection(selected, 1);
      } else if (k === 'k' || k === 'K') {
        e.preventDefault();
        moveSelection(selected, -1);
      } else if (k === 'Enter' && selected && (e.target as HTMLElement)?.tagName !== 'BUTTON') {
        setPanelOpen((p) => !p);
      } else if (k === 'A' && e.shiftKey) {
        e.preventDefault();
        openBatch(selected ? selected.approval.risk_level : 'L0');
      } else if (k === 'a' && selected) {
        e.preventDefault();
        approveSingle(selected);
      } else if ((k === 'r' || k === 'R') && selected) {
        e.preventDefault();
        setRejectDlg(selected);
      } else if ((k === 'e' || k === 'E') && selected) {
        e.preventDefault();
        setModifyDlg(selected);
      } else if (k === 'z' || k === 'Z') {
        const last = [...app.staged].reverse().find((b) => b.status === 'staged' && !b.immediate);
        if (last) void app.undo(last.batch_id);
      } else if (k === '/') {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  if (scope === 'none') return <p className="banner banner-info">{t('inbox.noInbox')}</p>;
  if (inbox.loading && !inbox.data) return <Loading />;
  if (inbox.error) return <ErrorBox error={inbox.error} />;

  const requestsWithItems = [...new Set((inbox.data ?? []).map((i) => i.approval.request_id!))];
  const activeStaged = app.staged.filter((b) => b.status === 'staged');
  const nextDeadline = (reqs.data ?? [])
    .filter((r) => r.request.due_date && !['submitted', 'archived', 'cancelled'].includes(r.request.status))
    .sort((a, b) => (a.request.due_date! < b.request.due_date! ? -1 : 1))[0];

  return (
    <div className="inbox">
      <div className="page-head">
        <h1>{t('inbox.title')}</h1>
        <div className="filters" role="search">
          <label>
            <span className="sr-only">{t('inbox.filter.request')}</span>
            <select value={requestFilter} onChange={(e) => setRequestFilter(e.target.value)} aria-label={t('inbox.filter.request')}>
              <option value="">{t('inbox.filter.all')}</option>
              {requestsWithItems.map((id) => (
                <option key={id} value={id}>
                  {reqMap.get(id)?.request.title ?? id}
                </option>
              ))}
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> {t('inbox.filter.mine')}
          </label>
          <input ref={searchRef} type="search" placeholder={`${t('inbox.search')} (/)`} aria-label={t('inbox.search')} value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      </div>
      {scope === 'l3_and_assigned' && <p className="muted">{t('inbox.scopeExecutive')}</p>}
      {shortcuts && <p className="muted small">{t('inbox.keys')}</p>}

      {activeStaged.map((b) => (
        <div key={b.batch_id} className="banner banner-ok staged-bar" role="status">
          <span>
            {t('batch.staged', { n: b.approval_ids.length, time: formatCountdown(remainingMs(b, app.now)) })} <span className="muted mono small">{b.batch_id}</span>
          </span>
          {!b.immediate && (
            <button type="button" className="btn" onClick={() => void app.undo(b.batch_id)}>
              {t('batch.undo')} (Z)
            </button>
          )}
        </div>
      ))}

      <div className={`inbox-grid ${panelOpen && selected ? 'with-panel' : ''}`}>
        <div className="inbox-list">
          {items.length === 0 ? (
            <div className="empty">{nextDeadline ? t('inbox.empty', { title: nextDeadline.request.title, date: formatDate(nextDeadline.request.due_date, locale) }) : t('inbox.emptyNoDeadline')}</div>
          ) : (
            LEVELS.map((l) => (
              <Group
                key={l}
                level={l}
                items={groups[l]}
                expanded={expanded.has(l)}
                onToggle={() => toggleGroup(l)}
                evaluation={evaluations[l]}
                onBatch={() => openBatch(l)}
                selectedId={selectedId}
                onSelect={select}
                viewed={viewed}
                rowError={rowError}
                onForward={() => app.notify(t('inbox.forwarded'), 'success')}
                canRoleApprove={canApprove(role, l)}
              />
            ))
          )}
        </div>
        {panelOpen && selected && (
          <aside className="inbox-panel" aria-label={t('lineage.title')}>
            <EvidencePanel
              key={selected.approval.id}
              answer={selected.answer}
              questionText={selected.questions.map((q) => `${q.native_id ?? ''} · ${pickText(q.text, locale)}`).join(' / ')}
              actions={<PanelActions it={selected} onApprove={() => approveSingle(selected)} onReject={() => setRejectDlg(selected)} onModify={() => setModifyDlg(selected)} />}
            >
              <PanelConfirmations it={selected} ack={ack} setAck={setAck} warnNotes={warnNotes} setWarnNotes={setWarnNotes} />
              {rowError && rowError.id === selected.approval.id && (
                <div className="banner banner-danger" role="alert">
                  {rowError.text}
                  {rowError.roleIssue && (
                    <button type="button" className="btn btn-link" onClick={() => app.notify(t('inbox.forwarded'), 'success')}>
                      {t('inbox.forward')}
                    </button>
                  )}
                </div>
              )}
            </EvidencePanel>
          </aside>
        )}
      </div>

      {batchDlg && (
        <BatchDialog
          evaluation={batchDlg}
          cpNames={cpNames}
          reqMap={reqMap}
          onClose={() => setBatchDlg(null)}
          onConfirm={async (immediate) => {
            const eligible = batchDlg.eligible;
            const ids = eligible.map((i) => i.approval.id);
            const estimates = eligible.filter((i) => i.answer.is_estimate).map((i) => i.approval.id);
            const notes = [...new Set(ids.map((id) => (warnNotes[id] ?? '').trim()).filter(Boolean))];
            setBatchDlg(null);
            try {
              // 06 §5.1：L2 须逐条看过详情（viewed_approval_ids）、估算须知悉（estimate_ack）；服务端复核
              const b = await app.stage(ids, {
                immediate,
                viewed: ids.filter((id) => viewed.has(id)),
                confirmations: {
                  estimate_ack: estimates.length ? estimates.every((id) => ack.has(id)) : undefined,
                  warn_note: notes.length ? notes.join(' / ') : undefined,
                },
              });
              if (b.status === 'staged') app.notify(t('batch.staged', { n: ids.length, time: formatCountdown(b.window_ms) }), 'success', b.batch_id);
              else app.notify(b.status === 'committed' ? t('batch.committed', { n: ids.length }) : t('batch.voided'), b.status === 'committed' ? 'success' : 'error');
            } catch (e) {
              app.notify(app.errorText(e), 'error');
            }
          }}
        />
      )}
      {rejectDlg && (
        <RejectDialog
          onClose={() => setRejectDlg(null)}
          onConfirm={async (reason) => {
            const it = rejectDlg;
            try {
              await ds.decide({ approval_id: it.approval.id, decision: 'rejected', comment: reason }, actor);
              markDone(it.approval.id);
              setRejectDlg(null);
              app.notify(t('inbox.rejected'), 'info');
              app.refresh();
            } catch (e) {
              setRejectDlg(null);
              setRowError({ id: it.approval.id, text: app.errorText(e), roleIssue: e instanceof ApiException && e.error.code === 'GRAPH_APPROVAL_REQUIRED' });
            }
          }}
        />
      )}
      {modifyDlg && (
        <ModifyDialog
          it={modifyDlg}
          onClose={() => setModifyDlg(null)}
          onDone={() => {
            markDone(modifyDlg.approval.id);
            setModifyDlg(null);
            app.notify(t('inbox.modified'), 'success');
            app.refresh();
          }}
          onError={(e) => {
            setRowError({ id: modifyDlg.approval.id, text: app.errorText(e), roleIssue: e instanceof ApiException && e.error.code === 'GRAPH_APPROVAL_REQUIRED' });
            setModifyDlg(null);
          }}
        />
      )}
      {l3Dlg && (
        <L3Dialog
          it={l3Dlg}
          onClose={() => setL3Dlg(null)}
          onConfirm={() => {
            const it = l3Dlg;
            setL3Dlg(null);
            approveSingle(it, { clause_read_ack: true, step_up_auth: true });
          }}
        />
      )}
    </div>
  );
}

function Group(props: {
  level: RiskLevel;
  items: InboxItem[];
  expanded: boolean;
  onToggle: () => void;
  evaluation: BatchEvaluation;
  onBatch: () => void;
  selectedId: string | null;
  onSelect: (it: InboxItem) => void;
  viewed: Set<string>;
  rowError: { id: string; text: string } | null;
  onForward: () => void;
  canRoleApprove: boolean;
}) {
  const { t, locale } = useI18n();
  const { level, items, expanded, evaluation: ev } = props;
  const headId = `grp-${level}`;
  if (items.length === 0) return null;
  const label = level === 'L0' ? t('inbox.approveAll', { n: ev.eligible.length }) : t('inbox.approveGroup', { n: ev.eligible.length });
  const reasonText = ev.blockers.map((b) => t(`batch.blocker.${b}` as MessageKey)).join(' · ');
  return (
    <section className={`group group-${level}`} aria-labelledby={headId}>
      <div className="group-head">
        <button type="button" className="group-toggle" aria-expanded={expanded} onClick={props.onToggle} id={headId} aria-label={`${t('inbox.groupToggle', { level })}: ${t(`risk.${level}` as MessageKey)} (${items.length})`}>
          <span aria-hidden="true" className="caret">
            {expanded ? '▾' : '▸'}
          </span>
          <RiskBadge level={level} withText /> <span className="count">({items.length})</span>
        </button>
        <div className="group-actions">
          {level === 'L3' ? (
            <span className="muted small">{t('inbox.l3Hint')}</span>
          ) : ev.showButton ? (
            <button type="button" className="btn btn-primary" onClick={props.onBatch} title="Shift+A">
              {label} <kbd>⇧A</kbd>
            </button>
          ) : (
            <span className="batch-off">
              <button type="button" className="btn" aria-disabled="true" disabled aria-describedby={`${headId}-why`}>
                {t('inbox.batchUnavailable')}
              </button>
              <span id={`${headId}-why`} className="muted small">
                {props.canRoleApprove ? reasonText : t('common.requiredRole', { roles: t(`role.${requiredRolesFor(level)[0]}` as MessageKey) })}
              </span>
            </span>
          )}
        </div>
      </div>
      {level !== 'L3' && (ev.excluded.length > 0 || ev.overflow > 0) && (
        <details className="excluded">
          <summary>
            {ev.excluded.length > 0 && t('inbox.excluded', { n: ev.excluded.length })}
            {ev.overflow > 0 && ` · ${t('inbox.overflow', { n: ev.overflow, max: ev.max_items })}`}
          </summary>
          <p className="small muted">{t('batch.reasonsTitle')}</p>
          <ul>
            {ev.excluded.map(({ item, reasons }) => (
              <li key={item.approval.id}>
                <span className="mono">{item.questions[0]?.native_id ?? item.answer.id}</span>: {reasons.map((r) => t(`batch.reason.${r}` as MessageKey)).join(', ')}
              </li>
            ))}
          </ul>
        </details>
      )}
      {expanded && (
        <ul className="rows" role="list">
          {items.map((it) => {
            const q = it.questions[0];
            const sel = it.approval.id === props.selectedId;
            const stale = isStale(it);
            const openConf = it.conflicts.filter((c) => c.status === 'open');
            return (
              <li key={it.approval.id} className={`row ${sel ? 'row-selected' : ''}`} aria-current={sel ? 'true' : undefined}>
                <button type="button" className="row-main" onClick={() => props.onSelect(it)}>
                  <span className="row-ref mono">
                    {truncate(it.request_title, 22)} · {q?.native_id}
                  </span>
                  <span className="row-text">{q ? truncate(pickText(q.text, locale), 80) : it.answer.datapoint_id}</span>
                  <span className="row-value">
                    <ValueQuad answer={it.answer} compact />
                  </span>
                </button>
                <span className="row-flags">
                  {it.answer.is_estimate && <Chip tone="warn">{t('inbox.estimate')}</Chip>}
                  {it.answer.legal_commitment && <Chip tone="danger">{t('inbox.legal')}</Chip>}
                  {stale && <Chip tone="warn">{t('inbox.stale')}</Chip>}
                  {openConf.map((c) => (
                    <Chip key={c.id} tone={c.severity === 'blocking' ? 'danger' : 'warn'}>
                      {c.rule_id}
                    </Chip>
                  ))}
                  {it.evidence.length > 0 && <span className="muted small">{t('inbox.evidenceCount', { n: it.evidence.length })}</span>}
                  {props.viewed.has(it.approval.id) && <span className="sr-only">{t('inbox.detailViewed')}</span>}
                </span>
                <button type="button" className="btn btn-small" onClick={() => props.onSelect(it)}>
                  {t('inbox.review')}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function PanelActions({ it, onApprove, onReject, onModify }: { it: InboxItem; onApprove: () => void; onReject: () => void; onModify: () => void }) {
  const { t } = useI18n();
  const { role } = useApp();
  const allowed = canApprove(role, it.approval.risk_level, { legal_commitment: it.answer.legal_commitment, customer_tier: it.customer_tier, required_role: it.approval.required_role });
  const roles = requiredRolesFor(it.approval.risk_level, { legal_commitment: it.answer.legal_commitment, customer_tier: it.customer_tier });
  const why = roles.length === 1 && roles[0] === 'executive' ? t('inbox.executiveOnly') : t('common.requiredRole', { roles: roles.map((r) => t(`role.${r}` as MessageKey)).join(' / ') });
  return (
    <>
      <button type="button" className="btn btn-primary" onClick={onApprove} disabled={!allowed} aria-describedby={!allowed ? 'why-approve' : undefined}>
        {t('inbox.approve')} <kbd>A</kbd>
      </button>
      <button type="button" className="btn btn-danger" onClick={onReject} disabled={!allowed}>
        {t('inbox.reject')} <kbd>R</kbd>
      </button>
      <button type="button" className="btn" onClick={onModify} disabled={!allowed}>
        {t('inbox.modify')} <kbd>E</kbd>
      </button>
      {!allowed && (
        <p id="why-approve" className="small muted role-hint">
          {why}
        </p>
      )}
    </>
  );
}

function PanelConfirmations({ it, ack, setAck, warnNotes, setWarnNotes }: { it: InboxItem; ack: Set<string>; setAck: (s: Set<string>) => void; warnNotes: Record<string, string>; setWarnNotes: (r: Record<string, string>) => void }) {
  const { t, locale } = useI18n();
  const id = it.approval.id;
  const warns = it.conflicts.filter((c) => c.status === 'open' && c.severity === 'warning');
  return (
    <div className="confirmations">
      {it.answer.is_estimate && (
        <label className="check">
          <input
            type="checkbox"
            checked={ack.has(id)}
            onChange={(e) => {
              const n = new Set(ack);
              if (e.target.checked) n.add(id);
              else n.delete(id);
              setAck(n);
            }}
          />{' '}
          {t('inbox.estimateAck')}
        </label>
      )}
      {warns.map((c) => (
        <p key={c.id} className="banner banner-warn small">
          {c.rule_id}: {pickText(c.explanation, locale)}
        </p>
      ))}
      {warns.length > 0 && it.approval.risk_level !== 'L0' && it.approval.risk_level !== 'L1' && (
        <label className="field">
          <span>{t('inbox.warnNote')}</span>
          <textarea rows={2} value={warnNotes[id] ?? ''} onChange={(e) => setWarnNotes({ ...warnNotes, [id]: e.target.value })} />
        </label>
      )}
      {isStale(it) && <p className="banner banner-warn small">{t('inbox.stale')}</p>}
      {it.request_due_date && <p className="small muted">{t('inbox.due', { date: formatDate(it.request_due_date, locale) })}</p>}
    </div>
  );
}

function BatchDialog({ evaluation, cpNames, reqMap, onClose, onConfirm }: { evaluation: BatchEvaluation; cpNames: Map<string, string>; reqMap: Map<string, RequestView>; onClose: () => void; onConfirm: (immediate: boolean) => void | Promise<void> }) {
  const { t } = useI18n();
  const { undoMinutes, now } = useApp();
  const s = evaluation.summary;
  const dueSoon = evaluation.eligible.some((it) => dueSoonerThanWindow(it.request_due_date, now, undoMinutes));
  const [immediate, setImmediate] = useState(false);
  const [armed, setArmed] = useState(false);
  const reqTitles = [...new Set(evaluation.eligible.map((it) => reqMap.get(it.approval.request_id!)?.request.title ?? it.request_title))];
  return (
    <Dialog
      title={t('batch.summaryTitle')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          {!armed ? (
            <button type="button" className="btn btn-primary" onClick={() => setArmed(true)}>
              {t('batch.confirm', { n: s.count })}
            </button>
          ) : (
            <button type="button" className="btn btn-primary btn-armed" onClick={() => onConfirm(immediate)} autoFocus>
              {t('common.confirm')} ✓
            </button>
          )}
        </>
      }
    >
      <p className="lead" role="status">
        {t('batch.summary', { n: s.count, changed: s.numbers_changed })}
      </p>
      <dl className="kv">
        <dt>{t('batch.levels')}</dt>
        <dd>
          {Object.entries(s.by_level).map(([l, n]) => (
            <span key={l}>
              <RiskBadge level={l as RiskLevel} /> × {n}{' '}
            </span>
          ))}
        </dd>
        <dt>{t('inbox.filter.request')}</dt>
        <dd>{reqTitles.join(', ')}</dd>
        <dt>{t('batch.customers')}</dt>
        <dd>{s.customers.map((c) => (c === 'all' ? t('answer.all') : cpNames.get(c) ?? c)).join(', ')}</dd>
        <dt>{t('batch.datapoints', { n: s.datapoints.length })}</dt>
        <dd className="mono small">
          {s.datapoints.slice(0, 8).join(', ')}
          {s.datapoints.length > 8 ? ` … (+${s.datapoints.length - 8})` : ''}
        </dd>
        <dt>{t('inbox.estimate')}</dt>
        <dd>{t('batch.estimates', { n: s.estimates })}</dd>
      </dl>
      {evaluation.overflow > 0 && <p className="small muted">{t('inbox.overflow', { n: evaluation.overflow, max: evaluation.max_items })}</p>}
      <p className="small">{t('batch.undoInfo', { min: undoMinutes })}</p>
      {dueSoon && (
        <label className="check">
          <input type="checkbox" checked={immediate} onChange={(e) => setImmediate(e.target.checked)} /> {t('batch.immediate')}
        </label>
      )}
    </Dialog>
  );
}

function RejectDialog({ onClose, onConfirm }: { onClose: () => void; onConfirm: (reason: string) => void }) {
  const { t } = useI18n();
  const [reason, setReason] = useState('');
  return (
    <Dialog
      title={t('reject.title')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn btn-danger" disabled={!reason.trim()} onClick={() => onConfirm(reason.trim())}>
            {t('inbox.reject')}
          </button>
        </>
      }
    >
      <label className="field">
        <span>{t('reject.reason')}</span>
        <textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} required aria-required="true" />
      </label>
    </Dialog>
  );
}

function ModifyDialog({ it, onClose, onDone, onError }: { it: InboxItem; onClose: () => void; onDone: () => void; onError: (e: unknown) => void }) {
  const { t, locale } = useI18n();
  const { ds, actor } = useApp();
  const evs = useAsync(() => ds.listEvidence(), []);
  const [source, setSource] = useState<'' | 'existing_evidence' | 'upload' | 'human_confirmation'>('');
  const [evRef, setEvRef] = useState('');
  const [blob, setBlob] = useState<{ sha: string; name: string } | null>(null);
  const [value, setValue] = useState('');
  const [note, setNote] = useState('');
  const fact = it.evidence[0]?.fact_key;
  const candidates = (evs.data ?? []).filter((e) => e.status === 'verified' && (e.fact_key === fact || e.datapoint_id === it.answer.datapoint_id) && !it.answer.evidence_refs?.includes(`${e.id}#v${e.version}`));
  const valid = (source === 'existing_evidence' && !!evRef) || (source === 'upload' && !!blob) || (source === 'human_confirmation' && /^-?\d+([.,]\d+)?$/.test(value.trim()) && note.trim().length > 0);
  const submit = async () => {
    try {
      await ds.decide(
        {
          approval_id: it.approval.id,
          decision: 'modified',
          modification: { source: source as 'existing_evidence', evidence_ref: evRef || undefined, blob_sha256: blob?.sha, value: value ? value.trim().replace(',', '.') : undefined, note: note || undefined },
        },
        actor,
      );
      onDone();
    } catch (e) {
      onError(e);
    }
  };
  return (
    <Dialog
      title={t('modify.title')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn btn-primary" disabled={!valid} onClick={submit} aria-describedby="modify-why">
            {t('common.save')}
          </button>
        </>
      }
    >
      <p>
        <ValueQuad answer={it.answer} compact />
      </p>
      <fieldset>
        <legend>{t('modify.chooseSource')}</legend>
        <label className="check">
          <input type="radio" name="src" checked={source === 'existing_evidence'} onChange={() => setSource('existing_evidence')} /> {t('modify.existing')}
        </label>
        {source === 'existing_evidence' && (
          <div className="indent">
            {candidates.length === 0 ? (
              <p className="muted small">{t('modify.noEvidence')}</p>
            ) : (
              <select value={evRef} onChange={(e) => setEvRef(e.target.value)} aria-label={t('modify.existing')}>
                <option value="">—</option>
                {candidates.map((e) => (
                  <option key={e.id} value={`${e.id}#v${e.version}`}>
                    {String(e.extracted_value)} {e.unit ?? ''} · {e.source_type} · {formatDate(e.created_at, locale)}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}
        <label className="check">
          <input type="radio" name="src" checked={source === 'upload'} onChange={() => setSource('upload')} /> {t('modify.upload')}
        </label>
        {source === 'upload' && (
          <div className="indent">
            <input
              type="file"
              aria-label={t('modify.upload')}
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) setBlob({ sha: await ds.uploadBlob(f), name: f.name });
              }}
            />
            {blob && <p className="small mono">{blob.sha.slice(0, 23)}…</p>}
          </div>
        )}
        <label className="check">
          <input type="radio" name="src" checked={source === 'human_confirmation'} onChange={() => setSource('human_confirmation')} /> {t('modify.human')}
        </label>
        {source === 'human_confirmation' && (
          <div className="indent grid2">
            <label className="field">
              <span>
                {t('modify.value')} {it.answer.unit ? `(${it.answer.unit})` : ''}
              </span>
              <input inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} />
            </label>
            <label className="field">
              <span>{t('modify.note')}</span>
              <input value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
          </div>
        )}
      </fieldset>
      {!valid && (
        <p id="modify-why" className="small muted" aria-live="polite">
          {t('modify.noSource')}
        </p>
      )}
    </Dialog>
  );
}

function L3Dialog({ it, onClose, onConfirm }: { it: InboxItem; onClose: () => void; onConfirm: () => void }) {
  const { t, locale } = useI18n();
  const [readAck, setReadAck] = useState(false);
  const [stepUp, setStepUp] = useState(false);
  const [second, setSecond] = useState(false);
  const q = it.questions[0];
  return (
    <Dialog
      title={t('l3.title')}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          {!second ? (
            <button type="button" className="btn btn-primary" disabled={!readAck || !stepUp} onClick={() => setSecond(true)}>
              {t('l3.confirm')}
            </button>
          ) : (
            <button type="button" className="btn btn-primary btn-armed" onClick={onConfirm} autoFocus>
              {t('common.confirm')} ✓
            </button>
          )}
        </>
      }
    >
      <p>
        <RiskBadge level="L3" withText /> {it.answer.legal_commitment && <Chip tone="danger">{t('inbox.legal')}</Chip>}
      </p>
      <figure className="clause">
        <figcaption>{t('l3.clause')}</figcaption>
        <blockquote>{q ? pickText(q.text, locale) : it.answer.datapoint_id}</blockquote>
      </figure>
      <p>
        <strong>{t('answer.value')}:</strong> <ValueQuad answer={it.answer} />
      </p>
      <label className="check">
        <input type="checkbox" checked={readAck} onChange={(e) => setReadAck(e.target.checked)} /> {t('l3.clauseAck')}
      </label>
      <p>
        {stepUp ? (
          <Chip tone="ok">✓ {t('l3.stepUpDone')}</Chip>
        ) : (
          <button type="button" className="btn" onClick={() => setStepUp(true)}>
            {t('l3.stepUp')}
          </button>
        )}
      </p>
      {second && (
        <p className="banner banner-warn" role="alert">
          {t('l3.secondConfirm')}
        </p>
      )}
    </Dialog>
  );
}
