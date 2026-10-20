/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  /** 角色 → Bearer 令牌："compliance_owner=demo-owner,executive=demo-exec,sales=demo-sales"（或 JSON） */
  readonly VITE_API_TOKENS?: string;
  /** 单一 Bearer 令牌（所有角色共用） */
  readonly VITE_API_TOKEN?: string;
  /** 开发模式（后端 --dev-auth）：角色 → X-Belegt-Actor，如 "compliance_owner=user:u_owner" */
  readonly VITE_DEV_ACTORS?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
