// Spec: 01 §3 全局信息架构（Eingang 默认首页；角色默认首页）、§5.2 不可见优于禁用、§5.3（跳转链接、一致的帮助入口 3.2.6）
import { useEffect, useState } from 'react';
import { useApp } from './state/store';
import { LOCALES, LOCALE_NAMES, useI18n, type MessageKey } from './i18n';
import { DEMO_ROLES, visiblePages, type Page } from './domain/permissions';
import { formatCountdown, remainingMs } from './domain/undo';
import { Dialog, isTypingTarget } from './components/common';
import { InboxPage } from './pages/InboxPage';
import { CardsPage } from './pages/CardsPage';
import { RequestsPage } from './pages/RequestsPage';
import { ConflictsPage } from './pages/ConflictsPage';
import { SuppliersPage } from './pages/SuppliersPage';
import { ExpiryPage } from './pages/ExpiryPage';
import type { Locale } from './domain/format';
import type { Role } from './types';

const PAGES: Record<Page, () => JSX.Element> = {
  inbox: InboxPage,
  cards: CardsPage,
  requests: RequestsPage,
  conflicts: ConflictsPage,
  suppliers: SuppliersPage,
  expiry: ExpiryPage,
};

export function App() {
  const { t, locale, setLocale } = useI18n();
  const app = useApp();
  const [help, setHelp] = useState(false);
  const pages = visiblePages(app.role);
  const allowed = pages.includes(app.route.page);
  const PageComp = PAGES[app.route.page] ?? RequestsPage;

  useEffect(() => {
    document.documentElement.lang = locale === 'zh' ? 'zh-Hans' : locale;
  }, [locale]);
  useEffect(() => {
    document.title = `${t(`nav.${app.route.page}` as MessageKey)} · Belegt`;
  }, [app.route.page, t]);
  useEffect(() => {
    if (!app.shortcuts) return;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e)) return;
      if (e.key === '?') setHelp(true);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [app.shortcuts]);

  return (
    <>
      <a href="#main" className="skip-link">
        {t('app.skip')}
      </a>
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true">
            B
          </span>
          <span>
            <strong>{t('app.title')}</strong> <span className="muted">{t('app.subtitle')}</span>
          </span>
        </div>
        <nav aria-label={t('nav.main')} className="nav">
          <ul>
            {pages.map((p) => (
              <li key={p}>
                <a
                  href={`#/${p}`}
                  aria-current={app.route.page === p ? 'page' : undefined}
                  onClick={(e) => {
                    e.preventDefault();
                    app.navigate(p);
                  }}
                >
                  {t(`nav.${p}` as MessageKey)}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="toolbar">
          <label>
            <span className="tb-label">{t('app.role')}</span>
            <select value={app.role} onChange={(e) => app.setRole(e.target.value as Role)} data-testid="role-select">
              {DEMO_ROLES.map((r) => (
                <option key={r} value={r}>
                  {t(`role.${r}` as MessageKey)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="tb-label">{t('app.language')}</span>
            <select value={locale} onChange={(e) => setLocale(e.target.value as Locale)} data-testid="locale-select">
              {LOCALES.map((l) => (
                <option key={l} value={l} lang={l === 'zh' ? 'zh-Hans' : l}>
                  {LOCALE_NAMES[l]}
                </option>
              ))}
            </select>
          </label>
          <label className="check tb-check">
            <input type="checkbox" checked={app.shortcuts} onChange={(e) => app.setShortcuts(e.target.checked)} /> <span className="tb-label">{t('app.shortcuts')}</span>
          </label>
          <button type="button" className="btn btn-small" onClick={() => setHelp(true)}>
            {t('app.help')}
          </button>
        </div>
      </header>
      <div className="subbar small muted">
        <span>{t('app.user', { name: app.userName })}</span>
        <span>{app.ds.mode === 'mock' ? t('app.mode.mock') : t('app.mode.http', { base: String(import.meta.env.VITE_API_BASE) })}</span>
      </div>
      <main id="main" tabIndex={-1}>
        {allowed ? <PageComp /> : <p className="banner banner-info">{t('common.noAccess')}</p>}
      </main>
      <div className="toasts" aria-live="polite" aria-atomic="false" role="status">
        {app.toasts.map((ts) => {
          const b = ts.batchId ? app.staged.find((x) => x.batch_id === ts.batchId && x.status === 'staged') : undefined;
          const text = b ? t('batch.staged', { n: b.approval_ids.length, time: formatCountdown(remainingMs(b, app.now)) }) : ts.text;
          return (
            <div key={ts.id} className={`toast toast-${ts.kind}`}>
              <span>{text}</span>
              {b && !b.immediate && (
                <button type="button" className="btn btn-small" onClick={() => app.undo(b.batch_id)}>
                  {t('batch.undo')}
                </button>
              )}
              <button type="button" className="icon-btn" aria-label={t('common.close')} onClick={() => app.dismissToast(ts.id)}>
                <span aria-hidden="true">✕</span>
              </button>
            </div>
          );
        })}
      </div>
      {help && (
        <Dialog title={t('help.title')} onClose={() => setHelp(false)}>
          <ul className="help-list">
            <li>{t('help.inbox')}</li>
            <li>{t('help.cards')}</li>
            <li>{t('help.conflicts')}</li>
            <li>{t('help.toggle')}</li>
          </ul>
        </Dialog>
      )}
    </>
  );
}
