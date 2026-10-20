// Spec: 01 §3.1 证据侧栏、§3.8 UI-08 证据图溯源视图（列表视图为图的等效替代）、AC-401-1（≤ 2 次点击到原文/计算明细）
// 数据：GET /graph/lineage/{ref}；Calculation 四元组 = factor_set_version、methodology_version、input_hash、engine_version（_baseline §B）
import type { Answer, Calculation, Evidence, FactorSet, Lineage, LineageNode } from '../types';
import { useApp, useAsync } from '../state/store';
import { pickText, useI18n, type MessageKey } from '../i18n';
import { formatDate, formatDateTime, formatDec, formatPeriod, shortHash } from '../domain/format';
import { Chip, ErrorBox, Loading, RiskBadge, ValueQuad, agentName } from './common';
import type { ReactNode } from 'react';

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function EvidenceNode({ ev, excerpt }: { ev: Evidence; excerpt?: string }) {
  const { t, locale } = useI18n();
  const loc = ev.locator;
  const where = loc.connector
    ? t('lineage.erp', { connector: loc.connector.connector_id, date: formatDate(loc.connector.fetched_at, locale) })
    : loc.cq_ref || loc.approval_ref
      ? t('lineage.human')
      : [loc.page ? t('lineage.page', { n: loc.page }) : '', loc.sheet && loc.cell ? t('lineage.cell', { sheet: loc.sheet, cell: loc.cell }) : ''].filter(Boolean).join(' · ');
  const shown = typeof ev.extracted_value === 'string' ? (ev.value_type === 'number' ? formatDec(ev.extracted_value, locale) : ev.extracted_value) : typeof ev.extracted_value === 'boolean' ? (ev.extracted_value ? t('answer.true') : t('answer.false')) : '…';
  return (
    <div className={`node node-evidence ${ev.trust_zone === 'quarantine' ? 'node-quarantine' : ''}`}>
      <div className="node-head">
        <strong>{t('lineage.evidence')}</strong> <Chip tone={ev.status === 'verified' ? 'ok' : ev.status === 'expired' ? 'danger' : 'warn'}>{t(`status.evidence.${ev.status}` as MessageKey)}</Chip>{' '}
        <span className="muted mono">{ev.source_type}</span>
        {ev.trust_zone === 'quarantine' && <Chip tone="warn">{t('lineage.quarantine')}</Chip>}
      </div>
      <dl className="kv">
        <Field label={t('answer.value')}>
          {shown}
          {ev.unit ? ` ${ev.unit}` : ''}
        </Field>
        {ev.period && <Field label={t('answer.period')}>{formatPeriod(ev.period, locale)}</Field>}
        <Field label={t('lineage.locator')}>
          <span>{where || '—'}</span> <span className="muted mono">{shortHash(loc.blob_sha256)}</span>
          {loc.bbox && <span className="muted mono"> bbox [{loc.bbox.join(', ')}]</span>}
        </Field>
        {ev.valid_until && <Field label={t('expiry.col.validUntil')}>{formatDate(ev.valid_until, locale)}</Field>}
        <Field label={t('lineage.confidence', { p: Math.round(ev.confidence * 100) })}>
          {ev.verification ? t('lineage.verification', { method: ev.verification.method }) : '—'}
        </Field>
      </dl>
      {excerpt && (
        <figure className="excerpt">
          <figcaption>{t('lineage.excerpt')}</figcaption>
          <p>
            <mark>{excerpt}</mark>
          </p>
        </figure>
      )}
    </div>
  );
}

function CalculationNode({ c }: { c: Calculation }) {
  const { t, locale } = useI18n();
  return (
    <div className="node node-calc">
      <div className="node-head">
        <strong>{t('lineage.calculation')}</strong> <span className="mono">{c.method_id}</span> <Chip tone={c.status === 'computed' ? 'ok' : 'warn'}>{c.status}</Chip>
      </div>
      <dl className="kv four-tuple">
        <Field label={t('lineage.factorSetVersion')}>
          <span className="mono">{c.factor_set_version}</span>
        </Field>
        <Field label={t('lineage.methodology')}>
          <span className="mono">{c.methodology_version}</span>
        </Field>
        <Field label={t('lineage.inputHash')}>
          <span className="mono" title={c.input_hash}>
            {shortHash(c.input_hash)}
          </span>
        </Field>
        <Field label={t('lineage.engine')}>
          <span className="mono">{c.engine_version}</span>
        </Field>
        <Field label={t('lineage.outputHash')}>
          <span className="mono" title={c.output_hash}>
            {shortHash(c.output_hash)}
          </span>
        </Field>
        <Field label={t('lineage.result')}>
          {formatDec(c.result.value, locale)} {c.result.unit}{' '}
          <span className="muted">
            ({c.result.rounding.mode}, {c.result.rounding.digits})
          </span>
        </Field>
      </dl>
      <table className="mini">
        <caption>{t('lineage.inputs')}</caption>
        <tbody>
          {c.inputs.map((i) => (
            <tr key={i.ref + i.name}>
              <th scope="row" className="mono">
                {i.name}
              </th>
              <td className="num">
                {formatDec(i.value, locale)} {i.unit}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {c.factors_used.length > 0 && (
        <table className="mini">
          <caption>{t('lineage.factors')}</caption>
          <tbody>
            {c.factors_used.map((f) => (
              <tr key={f.factor_key}>
                <th scope="row" className="mono">
                  {f.factor_key}
                </th>
                <td className="num">
                  {formatDec(f.value, locale)} {f.unit}
                </td>
                <td className="muted mono">{f.source_row_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function FactorSetNode({ fs }: { fs: FactorSet }) {
  const { t, locale } = useI18n();
  return (
    <div className="node node-fs">
      <div className="node-head">
        <strong>{t('lineage.factorSet')}</strong> <span className="mono">{fs.id}</span> {fs.status === 'deprecated' && <Chip tone="warn">{t('lineage.deprecated')}</Chip>}
      </div>
      <dl className="kv">
        <Field label={t('lineage.license')}>{fs.license}</Field>
        <Field label={t('lineage.published')}>{formatDate(fs.published_at, locale)}</Field>
        <Field label={t('answer.period')}>{formatPeriod({ start: fs.valid_from, end: fs.valid_until ?? '…' }, locale)}</Field>
        <Field label={t('lineage.rows')}>
          {fs.row_count} · <span className="mono">{shortHash(fs.rows_hash)}</span>
        </Field>
      </dl>
    </div>
  );
}

/** 层级列表：Answer → derived_from Calculation → cites Evidence / uses FactorSet（键盘与屏幕阅读器可遍历）。 */
function LineageTree({ lineage }: { lineage: Lineage }) {
  const { t } = useI18n();
  const byRef = new Map(lineage.nodes.map((n) => [n.ref, n]));
  const children = (ref: string) => lineage.edges.filter((e) => e.from === ref);
  const render = (node: LineageNode, depth: number, seen: Set<string>): ReactNode => {
    if (depth > 4 || seen.has(node.ref)) return null;
    const nextSeen = new Set(seen).add(node.ref);
    const kids = children(node.ref);
    return (
      <>
        {node.type === 'Calculation' && <CalculationNode c={node.object as Calculation} />}
        {node.type === 'Evidence' && <EvidenceNode ev={node.object as Evidence} excerpt={node.ui_excerpt} />}
        {node.type === 'FactorSet' && <FactorSetNode fs={node.object as FactorSet} />}
        {kids.length > 0 && (
          <ul role="group">
            {kids.map((e) => {
              const child = byRef.get(e.to);
              if (!child) return null;
              return (
                <li key={e.from + e.to} role="treeitem" aria-selected={false}>
                  <span className="edge">{t(`lineage.edge.${e.kind}` as MessageKey)} ▸</span>
                  {render(child, depth + 1, nextSeen)}
                </li>
              );
            })}
          </ul>
        )}
      </>
    );
  };
  const root = lineage.nodes.find((n) => n.type === 'Answer') ?? lineage.nodes[0];
  if (!root) return <p className="muted">{t('lineage.none')}</p>;
  return (
    <div className="lineage" role="tree" aria-label={t('lineage.listView')}>
      {render(root, 0, new Set())}
    </div>
  );
}

/** 答案详情：值、单位、期间、口径 + 溯源。 */
export function EvidencePanel({ answer, questionText, actions, children }: { answer: Answer; questionText?: string; actions?: ReactNode; children?: ReactNode }) {
  const { t, locale } = useI18n();
  const { ds } = useApp();
  const ref = `${answer.id}#v${answer.version}`;
  const lin = useAsync(() => ds.lineage(ref), [ref]);
  const agent = agentName(answer.created_by);
  return (
    <section className="panel" aria-label={t('lineage.title')}>
      {questionText && <p className="panel-question">{questionText}</p>}
      <div className="panel-value">
        <ValueQuad answer={answer} />
      </div>
      <div className="chips">
        {answer.risk_level && <RiskBadge level={answer.risk_level} withText />}
        {answer.is_estimate && <Chip tone="warn">{t('inbox.estimate')}</Chip>}
        {answer.legal_commitment && <Chip tone="danger">{t('inbox.legal')}</Chip>}
        {agent && <Chip tone="ai">{t('inbox.aiDraft', { agent })}</Chip>}
      </div>
      <dl className="kv">
        <Field label={t('answer.unit')}>{answer.unit ?? '—'}</Field>
        <Field label={t('answer.period')}>{formatPeriod(answer.period, locale)}</Field>
        <Field label={t('answer.boundary')}>
          <span className="mono">{answer.boundary}</span>
        </Field>
        {answer.consolidation && (
          <Field label={t('answer.consolidation')}>
            <span className="mono">{answer.consolidation}</span>
          </Field>
        )}
        <Field label={t('answer.customerScope')}>{answer.customer_scope === 'all' ? t('answer.all') : answer.customer_scope.join(', ')}</Field>
        <Field label={t('answer.version')}>
          v{answer.version} · {t(`status.answer.${answer.status}` as MessageKey)}
        </Field>
        <Field label={t('answer.createdBy')}>
          <span className="mono">{answer.created_by}</span> · {formatDateTime(answer.created_at, locale)}
        </Field>
        {answer.estimate_method && <Field label={t('answer.estimateMethod')}>{answer.estimate_method}</Field>}
        {answer.reused_from && (
          <Field label={t('answer.reusedFrom')}>
            <span className="mono">{answer.reused_from}</span>
          </Field>
        )}
      </dl>
      {answer.drafting_note && (
        <p className="ai-note">
          <span className="muted">{t('answer.draftingNote')}: </span>
          {pickText(answer.drafting_note, locale)}
        </p>
      )}
      {children}
      {actions && <div className="panel-actions">{actions}</div>}
      <h3 className="panel-sub">{t('lineage.title')}</h3>
      {lin.loading && !lin.data ? <Loading /> : lin.error ? <ErrorBox error={lin.error} /> : lin.data ? <LineageTree lineage={lin.data} /> : null}
    </section>
  );
}
