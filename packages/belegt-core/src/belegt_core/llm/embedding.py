# Spec: 04 §7.1（tier-E 嵌入，多语种 1024 维，可本地）、§2.3（hints：确定性检索预先算好的候选集）
"""确定性本地嵌入（tier-E 的离线替身）：字符 3-gram + 词元的特征哈希，1024 维，L2 归一化。

用于 question-parser 的 hints 检索（候选 DataPoint）与 MockLLM 的"嵌入近似"打分；生产可替换为真正的多语种
嵌入模型（实现 ``Embedder`` 协议即可），因 space_id 变化需并行重建（03 §6.6）。
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from functools import lru_cache
from typing import Protocol

DIM = 1024
_STOP = {
    "der", "die", "das", "und", "oder", "im", "in", "ihr", "ihre", "ihres", "ihrer", "sie", "des", "dem", "den", "ein",
    "eine", "einer", "zum", "zur", "von", "vom", "mit", "für", "fur", "fuer", "je", "pro", "wie", "hoch", "waren", "ist",
    "sind", "liegt", "gibt", "es", "bitte", "the", "of", "and", "or", "a", "an", "to", "for", "is", "are", "your", "you",
    "per", "by", "at", "on", "nach", "über", "uber", "laut", "falls", "zutreffend", "aktuell", "aktuelle", "aktuellen",
    "haben", "hat", "welchem", "welche", "welcher", "werden", "wurden", "diese", "dieser", "einen", "kurz", "do",
}


_CJK = "\u3400-\u4dbf\u4e00-\u9fff"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_CJK_STOP = set("的是否有请填写及和与或在为了吗呢")  # 只在单字时丢弃


def normalize(text: str) -> str:
    """小写、去变音、ß→ss；保留 a-z、0-9、% 与中日韩统一表意文字（中文题检索），其余为空格。"""
    t = unicodedata.normalize("NFKC", text).lower()
    t = unicodedata.normalize("NFKD", t).replace("ß", "ss")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(f"[^a-z0-9%{_CJK}]+", " ", t).strip()


def stem(w: str) -> str:
    for suf in ("ungen", "ung", "en", "er", "es", "e", "s", "n"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _cjk_tokens(run: str) -> list[str]:
    """中文无空格分词的确定性替身：字二元组（单字串保留单字）。"""
    if len(run) == 1:
        return [] if run in _CJK_STOP else [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for w in normalize(text).split():
        if _CJK_RUN.search(w):
            for part in re.split(f"([{_CJK}]+)", w):
                if not part:
                    continue
                if _CJK_RUN.fullmatch(part):
                    out.extend(_cjk_tokens(part))
                elif part not in _STOP and len(part) > 1:
                    out.append(stem(part))
        elif len(w) == 1 and w.isdigit() and out and out[-1].isalpha():
            out.append(out[-1] + w)  # "Scope 2" / "Kategorie 1"：单个数字并入前一词元（"scope2"），否则三个 Scope 无法区分
        elif w not in _STOP and len(w) > 1:
            out.append(stem(w))
    return out


def near(a: str, b: str) -> bool:
    """词元近似相等：相同、长词互为前缀，或长度 ≥ 5 时 Damerau 距离 ≤ 1（错别字："scpoe"≈"scope"、"emissoin"≈"emission"）。"""
    if a == b:
        return True
    if len(a) > 4 and len(b) > 4 and (a.startswith(b) or b.startswith(a)):
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


def fuzzy_cover(qtok: set[str], label_toks: set[str]) -> float:
    if not label_toks:
        return 0.0
    return sum(1 for t in label_toks if any(near(t, x) for x in qtok)) / len(label_toks)


class Embedder(Protocol):
    space_id: str

    def embed(self, text: str) -> list[float]: ...


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


class HashingEmbedder:
    space_id = "hash-3gram-1024@1"

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    @lru_cache(maxsize=8192)
    def _embed(self, text: str) -> tuple[float, ...]:
        v = [0.0] * self.dim
        toks = tokens(text)
        for t in toks:
            h = _h("w:" + t)
            v[h % self.dim] += 2.0 if (h >> 32) & 1 else -2.0
            padded = f"#{t}#"
            for i in range(len(padded) - 2):
                g = padded[i:i + 3]
                hg = _h("g:" + g)
                v[hg % self.dim] += 1.0 if (hg >> 32) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return tuple(x / n for x in v)

    def embed(self, text: str) -> list[float]:
        return list(self._embed(text))


def cosine(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))
