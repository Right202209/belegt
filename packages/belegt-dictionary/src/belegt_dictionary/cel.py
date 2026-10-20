# Spec: 02 §1.3（plausibility.expr 采用 CEL 子集）、§4.4（condition 表达式）
"""Static checks for the CEL subset used in ``validity_rule.plausibility[].expr`` and ``entries[].condition``.

Allowed: number/bool/string literals, ``value``, ``dp:`` identifiers, arithmetic, comparison,
logical operators, parentheses and the single function ``size()``. Evaluation happens in
``consistency-auditor`` (belegt-core); the dictionary only checks syntax and references.
"""
from __future__ import annotations

import re

DP_REF = re.compile(r"dp:[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,3}")
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<dp>dp:[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,3})
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<op>==|!=|<=|>=|&&|\|\||[-+*/%<>!()])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
""", re.X)
ALLOWED_IDENTS = {"value", "true", "false", "null", "size"}
FUNCTIONS = {"size"}


class ExprError(ValueError):
    pass


def tokenize(expr: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ExprError(f"unexpected character at {pos}: {expr[pos:pos + 10]!r}")
        kind = m.lastgroup or ""
        if kind != "ws":
            out.append((kind, m.group()))
        pos = m.end()
    return out


def check_expr(expr: str) -> list[str]:
    """Return the dp: ids referenced by ``expr``; raise ExprError for anything outside the subset."""
    toks = tokenize(expr)
    if not toks:
        raise ExprError("empty expression")
    depth = 0
    refs: list[str] = []
    for i, (k, v) in enumerate(toks):
        if k == "ident":
            if v not in ALLOWED_IDENTS:
                raise ExprError(f"identifier {v!r} not allowed")
            if v in FUNCTIONS and (i + 1 >= len(toks) or toks[i + 1][1] != "("):
                raise ExprError(f"{v} must be called")
            if v not in FUNCTIONS and i + 1 < len(toks) and toks[i + 1][1] == "(":
                raise ExprError(f"function {v!r} not allowed")
        elif k == "dp":
            refs.append(v)
        elif v == "(":
            depth += 1
        elif v == ")":
            depth -= 1
            if depth < 0:
                raise ExprError("unbalanced parentheses")
    if depth != 0:
        raise ExprError("unbalanced parentheses")
    return refs
