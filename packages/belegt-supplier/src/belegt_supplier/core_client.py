# Spec: 08 §6.1（与 belegt-core 只有 HTTP 契约）、§6.5（提交包推送）、§6.6（/supplier-inbound/v1/*）
"""与客户侧（belegt-core 或任何实现 08 契约的系统）交互的适配接口。

`CoreClient` 是 Python 协议；`HttpCoreClient` 走 08 定义的 HTTP 端点并以 HMAC 签名（`Belegt-Signature`）；
`InMemoryCoreClient` 用于测试与单机演示。本包**不**导入 belegt_core。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import ApprovalInfo, CounterpartyInfo
from .signing import WEBHOOK_HEADER, canonical_json, webhook_signature

INBOUND_SUBMISSIONS = "/supplier-inbound/v1/submissions"
INBOUND_QUESTIONS = "/supplier-inbound/v1/questions"
INBOUND_EVENTS = "/supplier-inbound/v1/events"  # 本包扩展：decline / link.opened / reminders.paused / status


class CoreDeliveryError(Exception):
    pass


class CoreClient(Protocol):
    def push_submission(self, package: dict[str, Any]) -> None: ...
    def push_question(self, question: dict[str, Any]) -> None: ...
    def push_event(self, event: str, payload: dict[str, Any]) -> None: ...
    def get_approval(self, approval_ref: str) -> ApprovalInfo | None: ...
    def get_counterparty(self, counterparty_id: str) -> CounterpartyInfo | None: ...


class InMemoryCoreClient:
    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.questions: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.approvals: dict[str, ApprovalInfo] = {}
        self.counterparties: dict[str, CounterpartyInfo] = {}
        self.fail = False

    def push_submission(self, package):
        if self.fail:
            raise CoreDeliveryError("simulated failure")
        self.submissions.append(package)

    def push_question(self, question):
        if self.fail:
            raise CoreDeliveryError("simulated failure")
        self.questions.append(question)

    def push_event(self, event, payload):
        if self.fail:
            raise CoreDeliveryError("simulated failure")
        self.events.append((event, payload))

    def get_approval(self, approval_ref):
        return self.approvals.get(approval_ref)

    def get_counterparty(self, counterparty_id):
        return self.counterparties.get(counterparty_id)


class HttpCoreClient:
    """标准库 urllib 实现。所有 POST 体为规范化 JSON，并附 `Belegt-Signature`。"""

    def __init__(self, base_url: str, webhook_secret: bytes, *, api_key: str | None = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = webhook_secret
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, path: str, payload: dict, extra_headers: dict | None = None) -> None:
        body = canonical_json(payload)
        req = urllib.request.Request(self.base_url + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header(WEBHOOK_HEADER, webhook_signature(self.secret, body))
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                if resp.status >= 300:
                    raise CoreDeliveryError(f"HTTP {resp.status}")
        except urllib.error.URLError as exc:
            raise CoreDeliveryError(str(exc)) from exc

    def _get(self, path: str) -> dict | None:
        req = urllib.request.Request(self.base_url + path, method="GET")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise CoreDeliveryError(str(exc)) from exc
        except urllib.error.URLError as exc:
            raise CoreDeliveryError(str(exc)) from exc

    def push_submission(self, package):
        self._post(INBOUND_SUBMISSIONS, package, {"Idempotency-Key": package.get("signature", "")[-43:]})

    def push_question(self, question):
        self._post(INBOUND_QUESTIONS, question)

    def push_event(self, event, payload):
        self._post(INBOUND_EVENTS, {"event": event, "payload": payload})

    def get_approval(self, approval_ref):
        oid, _, ver = approval_ref.partition("#v")
        d = self._get(f"/graph/objects/{oid}?version={ver}")
        if not d:
            return None
        obj = d.get("object", d)
        return ApprovalInfo(ref=approval_ref, subject_ref=obj["subject_ref"], status=obj["status"],
                            decision=obj.get("decision"), decided_by=obj.get("decided_by"),
                            policy_version=obj.get("policy_version"))

    def get_counterparty(self, counterparty_id):
        d = self._get(f"/graph/objects/{counterparty_id}")
        if not d:
            return None
        obj = d.get("object", d)
        contacts = obj.get("contacts") or []
        return CounterpartyInfo(counterparty_id=counterparty_id, name=obj.get("legal_name") or obj.get("name", ""),
                                domains=obj.get("domains") or [],
                                confirmed_addresses=[c["email"] for c in contacts if c.get("confirmed")],
                                country=obj.get("country"), timezone=obj.get("timezone") or "UTC")
