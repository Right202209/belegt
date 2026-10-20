// Spec: 07 §4.4/§4.6（API 从身份推出执行者；生产 OIDC Bearer）、belegt-core api/auth.py（开发静态令牌表）、
//       routes_workflow.DevActorMiddleware（仅 --dev-auth：X-Belegt-Actor → 令牌表中该执行者的令牌）
// 认证头：优先 Bearer 令牌（按角色或单一令牌），否则开发模式下退回 X-Belegt-Actor。
import type { Role } from '../types';

export interface AuthConfig {
  /** 角色 → Bearer 令牌（VITE_API_TOKENS="compliance_owner=demo-owner,executive=demo-exec,sales=demo-sales"） */
  tokens: Partial<Record<Role, string>>;
  /** 单一令牌（VITE_API_TOKEN），角色映射中没有时使用 */
  token?: string;
  /** 角色 → 开发执行者 ID（VITE_DEV_ACTORS="compliance_owner=user:u_owner,…"；仅后端 --dev-auth 时有效） */
  devActors: Partial<Record<Role, string>>;
}

/** "a=b,c=d" 或 JSON 对象 → Record。空值/格式错误的项被忽略。 */
export function parseRoleMap(raw?: string | null): Record<string, string> {
  const s = (raw ?? '').trim();
  if (!s) return {};
  if (s.startsWith('{')) {
    try {
      const j = JSON.parse(s) as Record<string, unknown>;
      return Object.fromEntries(Object.entries(j).filter(([, v]) => typeof v === 'string' && v) as [string, string][]);
    } catch {
      return {};
    }
  }
  const out: Record<string, string> = {};
  for (const part of s.split(',')) {
    const i = part.indexOf('=');
    if (i <= 0) continue;
    const k = part.slice(0, i).trim();
    const v = part.slice(i + 1).trim();
    if (k && v) out[k] = v;
  }
  return out;
}

export function authConfigFromEnv(env: Record<string, string | undefined>): AuthConfig {
  return {
    tokens: parseRoleMap(env.VITE_API_TOKENS) as AuthConfig['tokens'],
    token: env.VITE_API_TOKEN?.trim() || undefined,
    devActors: parseRoleMap(env.VITE_DEV_ACTORS) as AuthConfig['devActors'],
  };
}

/** 当前角色的认证头。令牌优先；没有令牌时用 X-Belegt-Actor（devActors 映射，或调用方给出的执行者 ID）。 */
export function authHeaders(cfg: AuthConfig, role: Role, fallbackActor?: string): Record<string, string> {
  const token = cfg.tokens[role] ?? cfg.token;
  if (token) return { Authorization: `Bearer ${token}` };
  const actor = cfg.devActors[role] ?? fallbackActor;
  return actor ? { 'X-Belegt-Actor': actor } : {};
}
