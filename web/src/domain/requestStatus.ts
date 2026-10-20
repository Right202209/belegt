// Spec: _baseline §E；03 §3.2 / 04 §3.1 状态机；01 §2.1 关键体验承诺（侧路用用户语言 + 唯一主按钮）
import type { RequestStatus } from '../types';

export const PIPELINE: RequestStatus[] = [
  'received',
  'triaged',
  'parsed',
  'mapped',
  'evidence_gathering',
  'drafting',
  'auditing',
  'in_review',
  'approved',
  'filling',
  'submitted',
  'archived',
];

export const SIDE_STATES: RequestStatus[] = ['awaiting_human', 'awaiting_supplier', 'halted', 'cancelled'];

export interface ParsedStatusReason {
  kind?: string; // awaiting_human:<cq|conflict|confirm> 或 halted:<reason>
  detail?: string;
  return_to?: RequestStatus;
}

/** 解析 `awaiting_human:<cq|conflict|confirm>;return_to=<state>` / `halted:<reason>;return_to=<state>`（03 §3.2 不变量）。 */
export function parseStatusReason(reason?: string): ParsedStatusReason {
  if (!reason) return {};
  const out: ParsedStatusReason = {};
  for (const part of reason.split(';')) {
    const [k, v] = part.split('=');
    if (k === 'return_to' && v) out.return_to = v as RequestStatus;
    else if (k.includes(':')) {
      const [kind, detail] = k.split(':');
      out.kind = kind;
      out.detail = detail;
    }
  }
  return out;
}

/** 流水线上"当前所在主状态"：侧路状态取 return_to，否则取自身。 */
export function pipelineIndex(status: RequestStatus, reason?: string): number {
  const i = PIPELINE.indexOf(status);
  if (i >= 0) return i;
  if (status === 'awaiting_supplier') return PIPELINE.indexOf('evidence_gathering');
  const rt = parseStatusReason(reason).return_to;
  return rt ? PIPELINE.indexOf(rt) : -1;
}
