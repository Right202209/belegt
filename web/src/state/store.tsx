// Spec: 01 §3（全局信息架构、角色默认首页）、06 §5.2（批次由服务端暂存；前端只显示撤销倒计时并调用撤销）
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Role } from '../types';
import type { DataSource } from '../api/types';
import { ApiException } from '../api/types';
import { MockDataSource } from '../api/mock/MockDataSource';
import { HttpDataSource } from '../api/HttpDataSource';
import { authConfigFromEnv, authHeaders } from '../api/auth';
import { USERS } from '../api/mock/generate';
import { defaultPage, visiblePages, type Page } from '../domain/permissions';
import { canUndo, fromServerBatch, isDue, type StagedBatch } from '../domain/undo';
import { APPROVAL_POLICY_V1, clampUndoWindowMinutes } from '../domain/policy';
import { useI18n } from '../i18n';

export interface Toast {
  id: number;
  text: string;
  kind: 'info' | 'success' | 'error';
  batchId?: string;
}

interface Route {
  page: Page;
  param?: string;
}

interface AppCtx {
  ds: DataSource;
  role: Role;
  setRole: (r: Role) => void;
  actor: string;
  userName: string;
  shortcuts: boolean;
  setShortcuts: (v: boolean) => void;
  route: Route;
  navigate: (page: Page, param?: string) => void;
  toasts: Toast[];
  notify: (text: string, kind?: Toast['kind'], batchId?: string) => void;
  dismissToast: (id: number) => void;
  staged: StagedBatch[];
  /** 服务端暂存（POST /approvals 批量体）；返回本地倒计时视图 */
  stage: (approvalIds: string[], opts: StageOptions) => Promise<StagedBatch>;
  undo: (batchId: string) => Promise<void>;
  removeStaged: (batchId: string, approvalIds: string[]) => Promise<void>;
  stagedIds: Set<string>;
  undoMinutes: number;
  now: number;
  refreshKey: number;
  refresh: () => void;
  errorText: (e: unknown) => string;
}

export interface StageOptions {
  immediate: boolean;
  /** L2：本会话中打开过详情的审批 ID（06 §5.1） */
  viewed: string[];
  confirmations?: { estimate_ack?: boolean; warn_note?: string };
}

const Ctx = createContext<AppCtx | null>(null);
const POLL_MS = 2000;

function createDataSource(getRole: () => Role, getActor: () => string, undoMinutes: number): DataSource {
  const env = import.meta.env as unknown as Record<string, string | undefined>;
  const base = env.VITE_API_BASE;
  if (base && base.trim()) {
    const auth = authConfigFromEnv(env);
    return new HttpDataSource(base.trim(), () => authHeaders(auth, getRole(), getActor()));
  }
  const m = new MockDataSource();
  m.undoWindowMinutes = undoMinutes;
  return m;
}

function parseHash(): Route | null {
  const h = window.location.hash.replace(/^#\/?/, '');
  if (!h) return null;
  const [page, ...rest] = h.split('/');
  return { page: page as Page, param: rest.length ? decodeURIComponent(rest.join('/')) : undefined };
}

function readUndoMinutes(): number {
  const q = new URLSearchParams(window.location.search).get('undoMinutes');
  return clampUndoWindowMinutes(q ? Number(q) : APPROVAL_POLICY_V1.batch_undo_window_minutes);
}

export function AppProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [role, setRoleState] = useState<Role>(() => {
    const r = new URLSearchParams(window.location.search).get('role');
    return r && ['compliance_owner', 'executive', 'sales', 'contributor'].includes(r) ? (r as Role) : 'compliance_owner';
  });
  const actorRef = useRef<string>(USERS.compliance_owner.id);
  const roleRef = useRef<Role>(role);
  const user = USERS[role as keyof typeof USERS] ?? USERS.compliance_owner;
  actorRef.current = user.id;
  roleRef.current = role;
  const undoMinutes = useMemo(readUndoMinutes, []);
  const ds = useMemo(() => createDataSource(() => roleRef.current, () => actorRef.current, undoMinutes), [undoMinutes]);
  const [shortcuts, setShortcuts] = useState(true);
  const [route, setRoute] = useState<Route>(() => parseHash() ?? { page: defaultPage(role) });
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [staged, setStaged] = useState<StagedBatch[]>([]);
  const [now, setNow] = useState(Date.now());
  const [refreshKey, setRefreshKey] = useState(0);
  const toastSeq = useRef(1);
  const polling = useRef(new Map<string, number>()); // batch_id → 上次查询时间
  const stagedRef = useRef<StagedBatch[]>([]);
  stagedRef.current = staged;

  const notify = useCallback((text: string, kind: Toast['kind'] = 'info', batchId?: string) => {
    const id = toastSeq.current++;
    setToasts((ts) => [...ts.filter((x) => !(batchId && x.batchId === batchId)), { id, text, kind, batchId }].slice(-4));
    if (!batchId) setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 6000);
  }, []);
  const dismissToast = useCallback((id: number) => setToasts((ts) => ts.filter((x) => x.id !== id)), []);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const navigate = useCallback((page: Page, param?: string) => {
    const hash = `#/${page}${param ? '/' + encodeURIComponent(param) : ''}`;
    if (window.location.hash !== hash) window.history.pushState(null, '', hash);
    setRoute({ page, param });
  }, []);

  useEffect(() => {
    const onPop = () => setRoute(parseHash() ?? { page: defaultPage(role) });
    window.addEventListener('popstate', onPop);
    window.addEventListener('hashchange', onPop);
    return () => {
      window.removeEventListener('popstate', onPop);
      window.removeEventListener('hashchange', onPop);
    };
  }, [role]);

  const setRole = useCallback(
    (r: Role) => {
      setRoleState(r);
      if (!visiblePages(r).includes(route.page)) navigate(defaultPage(r));
    },
    [route.page, navigate],
  );

  const errorText = useCallback(
    (e: unknown) => {
      if (e instanceof ApiException) {
        if (e.error.code === 'GRAPH_APPROVAL_REQUIRED') return t('inbox.roleError');
        if (e.error.code === 'GRAPH_NUM_UNREFERENCED') return t('modify.noSource');
        if (e.error.code === 'GRAPH_SUBMIT_BLOCKED') return t('inbox.submitBlocked');
        return t('common.error', { message: `${e.error.code}: ${e.error.message}` });
      }
      return t('common.error', { message: e instanceof Error ? e.message : String(e) });
    },
    [t],
  );

  const stage = useCallback(
    async (approvalIds: string[], opts: StageOptions) => {
      // 06 §5.2：服务端暂存（202 + undo_until），窗口结束由服务端复检后落图；前端不再自持窗口
      const sb = await ds.stageBatch(
        { approval_ids: approvalIds, viewed_approval_ids: opts.viewed, confirmations: opts.confirmations, immediate: opts.immediate },
        actorRef.current,
      );
      const b = fromServerBatch(sb, Date.now());
      setStaged((s) => [...s.filter((x) => x.batch_id !== b.batch_id), b]);
      if (b.status !== 'staged') refresh();
      return b;
    },
    [ds, refresh],
  );

  const undo = useCallback(
    async (batchId: string) => {
      const prev = stagedRef.current.find((b) => b.batch_id === batchId);
      if (!prev || !canUndo(prev, Date.now())) return;
      try {
        const sb = await ds.undoBatch(batchId, undefined, actorRef.current);
        setStaged((s) => s.map((b) => (b.batch_id === batchId ? fromServerBatch(sb, Date.now(), b) : b)));
        notify(t('batch.undone', { n: prev.approval_ids.length }), 'info');
      } catch (e) {
        notify(errorText(e), 'error');
      }
      setToasts((ts) => ts.filter((x) => x.batchId !== batchId));
      refresh();
    },
    [ds, notify, t, errorText, refresh],
  );

  const removeStaged = useCallback(
    async (batchId: string, ids: string[]) => {
      try {
        const sb = await ds.undoBatch(batchId, ids, actorRef.current);
        setStaged((s) => s.map((b) => (b.batch_id === batchId ? fromServerBatch(sb, Date.now(), b) : b)));
      } catch (e) {
        notify(errorText(e), 'error');
      }
      refresh();
    },
    [ds, notify, errorText, refresh],
  );

  // 每秒计时；倒计时到 0 → 向服务端查询批次结果（服务端读取时结算：committed / voided），未结算则每 2 s 再查
  useEffect(() => {
    const h = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(h);
  }, []);
  useEffect(() => {
    for (const b of staged) {
      if (!isDue(b, now)) continue;
      const last = polling.current.get(b.batch_id);
      if (last !== undefined && (last < 0 || now - last < POLL_MS)) continue;
      polling.current.set(b.batch_id, -1); // 进行中
      (async () => {
        try {
          const sb = await ds.getBatch(b.batch_id, actorRef.current);
          if (sb.status === 'staged') {
            polling.current.set(b.batch_id, Date.now());
            return;
          }
          polling.current.delete(b.batch_id);
          setStaged((s) => s.map((x) => (x.batch_id === b.batch_id ? fromServerBatch(sb, Date.now(), x) : x)));
          setToasts((ts) => ts.filter((x) => x.batchId !== b.batch_id));
          const ok = sb.status === 'committed';
          notify(ok ? t('batch.committed', { n: sb.approval_ids.length }) : t('batch.voided'), ok ? 'success' : 'error');
          refresh();
        } catch (e) {
          polling.current.set(b.batch_id, Date.now());
          notify(errorText(e), 'error');
        }
      })();
    }
  }, [now, staged, ds, notify, refresh, t, errorText]);

  const stagedIds = useMemo(() => new Set(staged.filter((b) => b.status === 'staged').flatMap((b) => b.approval_ids)), [staged]);

  const value: AppCtx = {
    ds,
    role,
    setRole,
    actor: user.id,
    userName: user.name,
    shortcuts,
    setShortcuts,
    route,
    navigate,
    toasts,
    notify,
    dismissToast,
    staged,
    stage,
    undo,
    removeStaged,
    stagedIds,
    undoMinutes,
    now,
    refreshKey,
    refresh,
    errorText,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error('AppProvider missing');
  return c;
}

/** 简单的异步加载 hook，随 refreshKey 自动重载。 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data?: T; error?: unknown; loading: boolean } {
  const { refreshKey } = useApp();
  const [state, setState] = useState<{ data?: T; error?: unknown; loading: boolean }>({ loading: true });
  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true }));
    fn().then(
      (data) => alive && setState({ data, loading: false }),
      (error) => alive && setState({ error, loading: false }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, refreshKey]);
  return state;
}
