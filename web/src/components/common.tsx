// Spec: 01 §3 全局约定（四元组显示、出处图标、风险三重编码 L0 ✓圆 / L1 ◆ / L2 ▲ / L3 ⬢）、§5.3 可访问性
import { useEffect, useRef, type ReactNode } from 'react';
import type { Answer, RiskLevel } from '../types';
import { useI18n, type MessageKey } from '../i18n';
import { formatDate, formatDec, formatPeriod, type Locale } from '../domain/format';

const RISK_ICON: Record<RiskLevel, string> = { L0: '✓', L1: '◆', L2: '▲', L3: '⬢' };

export function RiskBadge({ level, withText = false }: { level: RiskLevel; withText?: boolean }) {
  const { t } = useI18n();
  return (
    <span className={`risk risk-${level}`} title={t(`risk.${level}` as MessageKey)}>
      <span aria-hidden="true" className="risk-icon">
        {RISK_ICON[level]}
      </span>
      <span className="risk-code">{level}</span>
      {withText ? <span className="risk-text"> · {t(`risk.${level}` as MessageKey)}</span> : <span className="sr-only"> {t(`risk.${level}` as MessageKey)}</span>}
    </span>
  );
}

export function Chip({ children, tone = 'neutral', title }: { children: ReactNode; tone?: 'neutral' | 'ok' | 'warn' | 'danger' | 'info' | 'ai'; title?: string }) {
  return (
    <span className={`chip chip-${tone}`} title={title}>
      {children}
    </span>
  );
}

export function formatAnswerValue(a: Pick<Answer, 'value' | 'value_type' | 'unit'>, locale: Locale, t: (k: MessageKey) => string): string {
  switch (a.value_type) {
    case 'number':
      return `${formatDec(String(a.value), locale)}${a.unit && a.unit !== '1' ? ' ' + a.unit : ''}`;
    case 'bool':
      return a.value === true || a.value === 'true' ? t('answer.true') : t('answer.false');
    case 'date':
      return formatDate(String(a.value), locale);
    case 'file':
      return t('answer.file');
    case 'geo':
      return 'GeoJSON';
    default:
      return String(a.value);
  }
}

/** 四元组：值 + 单位 + 期间 + 口径，旁边带"出处"按钮（Enter/点击打开证据侧栏）。 */
export function ValueQuad({ answer, onSource, compact = false }: { answer: Answer; onSource?: () => void; compact?: boolean }) {
  const { t, locale } = useI18n();
  const v = formatAnswerValue(answer, locale, t);
  return (
    <span className={`quad ${compact ? 'quad-compact' : ''}`}>
      <span className="quad-value">{answer.value_type === 'text' && compact ? truncate(v, 28) : v}</span>
      {!compact && (
        <>
          <span className="quad-meta">{formatPeriod(answer.period, locale)}</span>
          <span className="quad-meta">{answer.boundary}</span>
        </>
      )}
      {onSource && (
        <button type="button" className="icon-btn" onClick={onSource} aria-label={t('lineage.open')} title={t('lineage.open')}>
          <span aria-hidden="true">ⓘ</span>
        </button>
      )}
    </span>
  );
}

export function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

export function Loading() {
  const { t } = useI18n();
  return (
    <p className="muted" role="status">
      {t('common.loading')}
    </p>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const { t } = useI18n();
  return (
    <div className="banner banner-danger" role="alert">
      {t('common.error', { message: error instanceof Error ? error.message : String(error) })}
    </div>
  );
}

/** 可访问的模态对话框：初始聚焦、Esc 关闭、Tab 循环、关闭后焦点归还。 */
export function Dialog({ title, onClose, children, footer, wide = false }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const { t } = useI18n();
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const focusables = () => Array.from(node?.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])') ?? []).filter((el) => !el.hasAttribute('disabled'));
    (focusables()[1] ?? focusables()[0])?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      } else if (e.key === 'Tab') {
        const f = focusables();
        if (!f.length) return;
        const first = f[0];
        const last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    node?.addEventListener('keydown', onKey);
    return () => {
      node?.removeEventListener('keydown', onKey);
      prev?.focus?.();
    };
  }, [onClose]);
  const id = useRef(`dlg-${Math.random().toString(36).slice(2)}`).current;
  return (
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`dialog ${wide ? 'dialog-wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={id} ref={ref} data-dialog="true">
        <div className="dialog-head">
          <h2 id={id}>{title}</h2>
          <button type="button" className="icon-btn" onClick={onClose} aria-label={t('common.close')}>
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer && <div className="dialog-foot">{footer}</div>}
      </div>
    </div>
  );
}

/** 进度条四段：已映射 / 有证据 / 已批准 / 已提交（01 §3.4）。 */
export function ProgressBar({ total, mapped, evidenced, approved, submitted, label }: { total: number; mapped: number; evidenced: number; approved: number; submitted: number; label: string }) {
  const pct = (n: number) => (total ? Math.round((n / total) * 100) : 0);
  return (
    <div className="progress" role="img" aria-label={label} title={label}>
      <div className="progress-seg seg-mapped" style={{ width: `${pct(mapped)}%` }} />
      <div className="progress-seg seg-evidenced" style={{ width: `${pct(evidenced)}%` }} />
      <div className="progress-seg seg-approved" style={{ width: `${pct(approved)}%` }} />
      <div className="progress-seg seg-submitted" style={{ width: `${pct(submitted)}%` }} />
    </div>
  );
}

/** 只要事件目标是输入控件就不触发单键快捷键。 */
export function isTypingTarget(e: KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable || !!el.closest('[data-dialog="true"]');
}

/** 01 §5.2：LLM Agent 产出的内容显示"KI-Entwurf"标签（确定性服务如 approval-router、calc-service 不标）。 */
const LLM_AGENTS = new Set(['intake-triage', 'question-parser', 'evidence-miner', 'answer-drafter', 'upstream-chaser', 'supplier-reply-reviewer', 'sales-assistant', 'filler']);
export function agentName(createdBy: string): string | undefined {
  const a = createdBy.startsWith('agent:') ? createdBy.slice(6) : undefined;
  return a && LLM_AGENTS.has(a) ? a : undefined;
}
