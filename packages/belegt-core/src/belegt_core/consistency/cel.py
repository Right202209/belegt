# Spec: 06 §1.2（规则 DSL：YAML 元数据 + CEL 表达式 + 确定性 Python 插件；CEL 中禁止 double，数值一律 dec()）
"""CEL 求值（``cel-python``）与 Decimal 函数库。

- 编译期检查：表达式中出现浮点字面量（``1.5``、``2e3``）即拒绝（NFR-02：CEL 中禁止 double 运算）；
  数值必须写成 ``dec('1.5')``。
- 函数：``dec(x)``（DEC-STR/int/Decimal → ``Decimal``）、``dec_eq(a, b, abs_tol, rel_tol)``（两者满足其一即相等，
  06 §2.1）、``dec_abs``、``dec_min``、``dec_max``、``dec_sum(list)``。
- CEL 非图灵完备、无副作用，求值必终止；任何求值错误抛 ``CelError``，由引擎按 fail-closed 处理（06 §1.3）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from typing import Any

try:  # cel-python
    import celpy  # type: ignore
    from celpy import celtypes  # type: ignore

    HAVE_CEL = True
except Exception:  # pragma: no cover - 依赖缺失时引擎 fail-closed
    celpy = None  # type: ignore
    celtypes = None  # type: ignore
    HAVE_CEL = False


class CelError(Exception):
    """CEL 编译或求值失败（fail-closed）。"""


_STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_FLOAT_RE = re.compile(r"(?<![A-Za-z_\d])\d+\.\d*(?:[eE][+-]?\d+)?|(?<![A-Za-z_\d])\d+[eE][+-]?\d+|(?<![A-Za-z_\d.])\.\d+")


def check_no_double(expr: str) -> None:
    stripped = _STRING_RE.sub("''", expr)
    m = _FLOAT_RE.search(stripped)
    if m:
        raise CelError(f"double literal {m.group(0)!r} not allowed in CEL; use dec('{m.group(0)}') (06 §1.2)")


def to_dec(x: Any) -> Decimal:
    if isinstance(x, bool):
        raise CelError("dec(): bool is not a number")
    if isinstance(x, Decimal):
        return x
    if isinstance(x, int):
        return Decimal(int(x))
    if isinstance(x, float):  # 只可能来自外部注入；拒绝
        raise CelError("dec(): double values are not allowed")
    try:
        return Decimal(str(x).strip())
    except (InvalidOperation, ValueError) as e:
        raise CelError(f"dec(): not a DEC-STR: {x!r}") from e


def dec_eq(a: Any, b: Any, abs_tol: Any, rel_tol: Any) -> bool:
    a, b, at, rt = to_dec(a), to_dec(b), to_dec(abs_tol), to_dec(rel_tol)
    d = abs(a - b)
    with localcontext() as c:
        c.prec = 34
        return d <= at or d <= rt * max(abs(a), abs(b))


def _b(v: bool) -> Any:
    return celtypes.BoolType(bool(v)) if HAVE_CEL else bool(v)


FUNCTIONS = {
    "dec": lambda x: to_dec(x),
    "dec_eq": lambda a, b, at, rt: _b(dec_eq(a, b, at, rt)),
    "dec_abs": lambda x: abs(to_dec(x)),
    "dec_min": lambda a, b: min(to_dec(a), to_dec(b)),
    "dec_max": lambda a, b: max(to_dec(a), to_dec(b)),
    "dec_sum": lambda xs: sum((to_dec(x) for x in xs), Decimal(0)),
}


def _to_cel(v: Any) -> Any:
    if not HAVE_CEL:
        return v
    if isinstance(v, bool):
        return celtypes.BoolType(v)
    if isinstance(v, Decimal):
        return celtypes.StringType(format(v, "f"))
    if isinstance(v, int):
        return celtypes.IntType(v)
    if isinstance(v, float):
        raise CelError("double values are not allowed in CEL activations")
    if isinstance(v, str):
        return celtypes.StringType(v)
    if v is None:
        return None
    if isinstance(v, dict):
        return celtypes.MapType({celtypes.StringType(str(k)): _to_cel(x) for k, x in v.items() if x is not None})
    if isinstance(v, (list, tuple)):
        return celtypes.ListType([_to_cel(x) for x in v])
    return celtypes.StringType(str(v))


class CompiledExpr:
    def __init__(self, source: str) -> None:
        self.source = source
        check_no_double(source)
        if not HAVE_CEL:
            raise CelError("cel-python is not installed")
        env = celpy.Environment()
        try:
            ast = env.compile(source)
            self._prog = env.program(ast, functions=FUNCTIONS)
        except Exception as e:  # celpy.CELParseError 等
            raise CelError(f"CEL compile error in {source!r}: {e}") from e

    def evaluate(self, activation: dict[str, Any]) -> Any:
        act = {k: _to_cel(v) for k, v in activation.items() if v is not None}
        try:
            out = self._prog.evaluate(act)
        except Exception as e:
            raise CelError(f"CEL evaluation error in {self.source!r}: {e}") from e
        if HAVE_CEL and isinstance(out, Exception):  # celpy 可能返回 CELEvalError 对象
            raise CelError(f"CEL evaluation error in {self.source!r}: {out}")
        return out

    def evaluate_bool(self, activation: dict[str, Any]) -> bool:
        out = self.evaluate(activation)
        if not isinstance(out, (bool,)) and not (HAVE_CEL and isinstance(out, celtypes.BoolType)):
            raise CelError(f"CEL expression {self.source!r} did not return bool (got {type(out).__name__})")
        return bool(out)


@lru_cache(maxsize=512)
def compile_expr(source: str) -> CompiledExpr:
    return CompiledExpr(source)


__all__ = ["CelError", "CompiledExpr", "compile_expr", "check_no_double", "dec_eq", "to_dec", "FUNCTIONS", "HAVE_CEL"]
