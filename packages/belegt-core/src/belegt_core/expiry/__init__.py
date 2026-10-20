# Spec: 06 §7（FR-404 有效期预警：90/60/30/14/7/0 天梯度、通知对象、动作）、03 §3.5（verified → expired 由
#       consistency-auditor 有效期扫描写入；expired 证据不能被新 Answer 引用）、04 §2.6
"""有效期预警（T3 每日批量的一部分）。

    alerts = scan_expiry(store, org_id)                 # 只读：梯度提醒列表
    res = apply_expiry(store, org_id)                   # 到期（≤0 天）且已过 valid_until 的 verified Evidence → expired
"""
from .scanner import TIERS, ExpiryAlert, apply_expiry, expiry_tier, scan_expiry

__all__ = ["scan_expiry", "apply_expiry", "expiry_tier", "ExpiryAlert", "TIERS"]
