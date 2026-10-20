# Spec: 04 §6.1（测试与样例数据）；与 belegt_io.workbooks.build_sample_pdf 相同的极简 PDF 结构，扩展为多页
"""确定性极简 PDF 生成器（纯标准库，字节级可复现）：多页、Helvetica、WinAnsi，每行一个 `Tm ... Tj`。

只用于样例与测试数据（电费单、燃气单、证书）。`line_bbox()` 给出与 belegt_io 相同口径的行 bbox 估算，
供 belegt-bench 的定位黄金标注使用（PDF 点坐标 `[x0, y0, x1, y1]`）。
"""
from __future__ import annotations

from pathlib import Path

PAGE_W, PAGE_H = 595, 842
X0, Y_TOP, LEADING, FONT_SIZE = 72, 780, 18, 11


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def line_y(index: int) -> float:
    return float(Y_TOP - index * LEADING)


def line_bbox(index: int, text: str, size: float = FONT_SIZE) -> tuple[float, float, float, float]:
    """第 index 行（0 起）的估算 bbox：x1 = x0 + 0.5·size·len(text)（平均字宽 0.5 em）。"""
    y = line_y(index)
    return (float(X0), round(y - 0.2 * size, 1), round(X0 + 0.5 * size * len(text), 1), round(y + 0.8 * size, 1))


def build_pdf(pages: list[list[str]], *, info: dict[str, str] | None = None) -> bytes:
    """pages：每页若干行文本（latin-1 可编码）。info：写入 /Info 字典（如 canary）。"""
    n = len(pages)
    objs: list[bytes] = [b"", b""]  # 1 Catalog, 2 Pages（占位）
    font_id = 3
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for lines in pages:
        ops = ["BT", f"/F1 {FONT_SIZE} Tf"]
        for i, ln in enumerate(lines):
            ops.append(f"1 0 0 1 {X0} {line_y(i):g} Tm ({_esc(ln)}) Tj")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content_id = len(objs) + 1
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        page_id = len(objs) + 1
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode())
        kids.append(page_id)
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {n} >>".encode()
    info_id = None
    if info:
        body = " ".join(f"/{k} ({_esc(v)})" for k, v in sorted(info.items()))
        objs.append(f"<< {body} >>".encode("latin-1"))
        info_id = len(objs)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"/Size {len(objs) + 1} /Root 1 0 R" + (f" /Info {info_id} 0 R" if info_id else "")
    out += f"trailer\n<< {trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_pdf(path: str | Path, pages: list[list[str]], **kw: object) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(build_pdf(pages, **kw))  # type: ignore[arg-type]
    return p
