# Spec: 04 §5.3（循环检测 LD-01…LD-05）、§5.1 GP-10
"""循环检测。

| ID | 信号 | 阈值 | 动作 |
|---|---|---|---|
| LD-01 | 同一 ``(actor, tool, args_hash)`` 重复调用 | 3 次 / Activity | ``LoopDetected`` → halt |
| LD-02 | 同一对象被同一规则拒绝（同错误码） | 3 次 | halt，转人工 |
| LD-03 | Request 状态振荡（同一对状态往返） | > 3 次 | halt |
| LD-04 | LangGraph 节点访问次数 | 超 ``recursion_limit`` | ``LoopDetected`` |
| LD-05 | 同一 Question 的 Mapping 被人改判 | > 2 次 | 不 halt，进评测难例集 |
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict
from typing import Any

from ..canonical import hash_obj


class LoopDetected(Exception):
    def __init__(self, rule_id: str, message: str, signature: str | None = None) -> None:
        super().__init__(f"{rule_id}: {message}")
        self.rule_id, self.signature = rule_id, signature
        self.reason = "loop"


class LoopDetector:
    LD01_MAX = 3
    LD02_MAX = 3
    LD03_MAX = 3
    LD05_MAX = 2

    def __init__(self) -> None:
        self._calls: dict[str, Counter[str]] = defaultdict(Counter)
        self._rejections: Counter[tuple[str, str]] = Counter()
        self._status_pairs: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
        self._last_status: dict[str, str] = {}
        self._overrides: Counter[str] = Counter()
        self.hard_examples: list[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def signature(actor: str, tool: str, args: Any) -> str:
        return hash_obj({"actor": actor, "tool": tool, "args": args})

    def call(self, activity_scope: str, actor: str, tool: str, args: Any) -> str:
        """LD-01：返回 ``loop_sig``；同一签名在同一 Activity 内第 4 次出现抛 ``LoopDetected``。"""
        sig = self.signature(actor, tool, args)
        with self._lock:
            self._calls[activity_scope][sig] += 1
            n = self._calls[activity_scope][sig]
        if n > self.LD01_MAX:
            raise LoopDetected("LD-01", f"{actor} called {tool} with identical args {n} times in {activity_scope}", sig)
        return sig

    def end_activity(self, activity_scope: str) -> None:
        with self._lock:
            self._calls.pop(activity_scope, None)

    def rejection(self, object_id: str, code: str) -> int:
        """LD-02：同一对象同一错误码第 3 次拒绝抛 ``LoopDetected``。"""
        with self._lock:
            self._rejections[(object_id, code)] += 1
            n = self._rejections[(object_id, code)]
        if n >= self.LD02_MAX:
            raise LoopDetected("LD-02", f"{object_id} rejected {n} times with {code}")
        return n

    def status(self, request_id: str, new_status: str) -> None:
        """LD-03：同一对状态往返 > 3 次抛 ``LoopDetected``（侧路 awaiting_human/halted 不计）。"""
        if new_status in ("awaiting_human", "halted"):
            return
        with self._lock:
            prev = self._last_status.get(request_id)
            self._last_status[request_id] = new_status
            if prev is None or prev == new_status:
                return
            pair = tuple(sorted((prev, new_status)))
            self._status_pairs[request_id][pair] += 1  # type: ignore[index]
            n = self._status_pairs[request_id][pair]  # type: ignore[index]
        if n > 2 * self.LD03_MAX:  # 往返 = 两次跳变
            raise LoopDetected("LD-03", f"request {request_id} oscillates {pair[0]} <-> {pair[1]}")

    def mapping_override(self, question_id: str) -> bool:
        """LD-05：返回是否已进入难例集（不 halt）。"""
        with self._lock:
            self._overrides[question_id] += 1
            if self._overrides[question_id] > self.LD05_MAX and question_id not in self.hard_examples:
                self.hard_examples.append(question_id)
                return True
        return False

    @staticmethod
    def from_recursion(exc: Exception, graph: str) -> LoopDetected:
        """LD-04：把 LangGraph 的 ``GraphRecursionError`` 映射为 ``LoopDetected``。"""
        return LoopDetected("LD-04", f"{graph}: recursion limit exceeded ({exc})")
