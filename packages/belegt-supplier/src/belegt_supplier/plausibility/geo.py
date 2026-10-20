# Spec: 08 §5.2 PLAUS-015…019（RFC 7946 GeoJSON、WGS 84、面积、陆地/国境）
"""GeoJSON 校验与面积计算（纯标准库）。

面积：球面近似（WGS 84 等积球半径 6 371 007.2 m），相对椭球面积误差 < 0.5%，足以判定 4 ha / 0.01 ha / 10 000 ha 阈值；
如需严格椭球面积可替换 `polygon_area_m2`。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterator

AUTHALIC_R = 6371007.2
NUM_RE = re.compile(r"-?\d+(?:\.(\d+))?(?:[eE][-+]?\d+)?")


class GeoError(ValueError):
    pass


def load_geojson(value: Any) -> tuple[dict, str | None]:
    """返回 (对象, 原文)。value 可为 dict 或 JSON 字符串。"""
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            return json.loads(value), value
        except json.JSONDecodeError as exc:
            raise GeoError(f"invalid JSON: {exc}") from exc
    raise GeoError("geolocation must be GeoJSON")


def geometries(obj: dict) -> Iterator[tuple[dict, dict]]:
    """遍历 (geometry, properties)。"""
    t = obj.get("type")
    if t == "FeatureCollection":
        for f in obj.get("features") or []:
            yield from geometries(f)
    elif t == "Feature":
        g = obj.get("geometry")
        if not isinstance(g, dict):
            raise GeoError("Feature without geometry")
        props = obj.get("properties") or {}
        if g.get("type") == "GeometryCollection":
            for sub in g.get("geometries") or []:
                yield sub, props
        else:
            yield g, props
    elif t in ("Point", "MultiPoint", "Polygon", "MultiPolygon"):
        yield obj, {}
    else:
        raise GeoError(f"unsupported GeoJSON type: {t}")


def _positions(g: dict) -> Iterator[list]:
    t, c = g.get("type"), g.get("coordinates")
    if t == "Point":
        yield c
    elif t == "MultiPoint":
        yield from c
    elif t == "Polygon":
        for ring in c:
            yield from ring
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                yield from ring


def _segments_intersect(p1, p2, p3, p4) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-18 else (1 if v > 0 else -1)

    def on_seg(a, b, c):
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    o1, o2, o3, o4 = orient(p1, p2, p3), orient(p1, p2, p4), orient(p3, p4, p1), orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    return (o1 == 0 and on_seg(p1, p2, p3)) or (o2 == 0 and on_seg(p1, p2, p4)) or \
           (o3 == 0 and on_seg(p3, p4, p1)) or (o4 == 0 and on_seg(p3, p4, p2))


def ring_self_intersects(ring: list) -> bool:
    n = len(ring) - 1  # 闭合环，最后一点 = 第一点
    segs = [(ring[i], ring[i + 1]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # 相邻边共享端点
            if _segments_intersect(*segs[i], *segs[j]):
                return True
    return False


def validate(obj: dict, raw: str | None = None, min_decimals: int = 6) -> list[str]:
    """PLAUS-015：返回问题列表（空 = 合法）。"""
    problems: list[str] = []
    try:
        geoms = list(geometries(obj))
    except GeoError as exc:
        return [str(exc)]
    if not geoms:
        return ["no geometry"]
    for g, _props in geoms:
        t = g.get("type")
        c = g.get("coordinates")
        if c is None:
            problems.append(f"{t}: coordinates missing")
            continue
        try:
            pos = list(_positions(g))
        except (TypeError, IndexError):
            problems.append(f"{t}: malformed coordinates")
            continue
        for p in pos:
            if not (isinstance(p, list) and len(p) >= 2 and all(isinstance(x, (int, float)) for x in p[:2])):
                problems.append(f"{t}: position must be [lon, lat]")
                break
            lon, lat = p[0], p[1]
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                problems.append(f"{t}: out of WGS 84 range ({lon},{lat})")
                break
        if t in ("Polygon", "MultiPolygon"):
            polys = [c] if t == "Polygon" else c
            for poly in polys:
                for ring in poly:
                    if len(ring) < 4:
                        problems.append("ring has < 4 positions")
                    elif ring[0][:2] != ring[-1][:2]:
                        problems.append("ring not closed")
                    elif ring_self_intersects([p[:2] for p in ring]):
                        problems.append("ring self-intersects")
    # 小数位：优先检查原文（JSON 解析会丢失尾随 0）
    if raw is not None:
        decs = [len(m.group(1) or "") for m in NUM_RE.finditer(_coords_text(raw))]
    else:
        decs = []
        for g, _ in geoms:
            for p in _positions(g):
                for x in p[:2]:
                    s = repr(float(x))
                    decs.append(len(s.split(".")[1]) if "." in s and "e" not in s else 0)
    if decs and min(decs) < min_decimals:
        problems.append(f"coordinates need at least {min_decimals} decimals")
    return problems


def _coords_text(raw: str) -> str:
    """抽出原文中所有 "coordinates": [...] 片段。"""
    out = []
    for m in re.finditer(r'"coordinates"\s*:\s*', raw):
        i, depth, start = m.end(), 0, m.end()
        while i < len(raw):
            if raw[i] == "[":
                depth += 1
            elif raw[i] == "]":
                depth -= 1
                if depth == 0:
                    out.append(raw[start:i + 1])
                    break
            i += 1
    return " ".join(out)


def ring_area_m2(ring: list) -> float:
    if len(ring) < 4:
        return 0.0
    total = 0.0
    for i in range(len(ring) - 1):
        lon1, lat1 = map(math.radians, ring[i][:2])
        lon2, lat2 = map(math.radians, ring[i + 1][:2])
        total += (lon2 - lon1) * (2 + math.sin(lat1) + math.sin(lat2))
    return abs(total) * AUTHALIC_R ** 2 / 2


def polygon_area_m2(g: dict) -> float:
    t, c = g.get("type"), g.get("coordinates")
    polys = [c] if t == "Polygon" else (c if t == "MultiPolygon" else [])
    area = 0.0
    for poly in polys:
        if not poly:
            continue
        area += ring_area_m2(poly[0]) - sum(ring_area_m2(h) for h in poly[1:])
    return area


def centroid(g: dict) -> tuple[float, float]:
    pts = [p[:2] for p in _positions(g)]
    if g.get("type") in ("Polygon", "MultiPolygon"):
        pts = pts[:-1] or pts
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return lon, lat


def declared_area_ha(props: dict) -> float | None:
    for key in ("area_ha", "areaHa", "area", "Area"):
        if key in props:
            try:
                return float(props[key])
            except (TypeError, ValueError):
                return None
    return None


class BBoxGeoOracle:
    """内置粗粒度判定器（离线、零依赖）：国家外接矩形 + 少量大洋样区。

    - 落在已知大洋样区 → is_land=False；落在某国外接矩形 → True；其他 → None（不判定）。
    - in_country：国家外接矩形（+缓冲）内 → True，之外 → False，未收录国家 → None。
    生产环境应注入基于 Natural Earth 1:10m 陆地面与行政边界的实现（08 §5.2）。
    """

    COUNTRIES: dict[str, tuple[float, float, float, float]] = {  # (min_lon, min_lat, max_lon, max_lat)，近似
        "BR": (-74.0, -33.8, -34.7, 5.3), "ID": (95.0, -11.0, 141.0, 6.1), "MY": (99.6, 0.85, 119.3, 7.4),
        "CI": (-8.6, 4.3, -2.5, 10.8), "GH": (-3.3, 4.7, 1.2, 11.2), "CO": (-79.0, -4.3, -66.8, 12.5),
        "PE": (-81.4, -18.4, -68.6, -0.03), "EC": (-81.1, -5.0, -75.2, 1.7), "VN": (102.1, 8.4, 109.5, 23.4),
        "TH": (97.3, 5.6, 105.7, 20.5), "CM": (8.4, 1.6, 16.2, 13.1), "NG": (2.6, 4.2, 14.7, 13.9),
        "PG": (140.8, -11.7, 156.0, -1.3), "HN": (-89.4, 12.9, -83.1, 16.5), "GT": (-92.3, 13.7, -88.2, 17.8),
        "ET": (33.0, 3.4, 48.0, 14.9), "UG": (29.5, -1.5, 35.0, 4.2), "IN": (68.1, 6.7, 97.4, 35.5),
        "CN": (73.5, 18.2, 134.8, 53.6), "TR": (26.0, 35.8, 44.8, 42.1), "DE": (5.9, 47.3, 15.0, 55.1),
        "PY": (-62.7, -27.6, -54.3, -19.3), "AR": (-73.6, -55.1, -53.6, -21.8), "BO": (-69.6, -22.9, -57.5, -9.7),
    }
    OCEANS: list[tuple[float, float, float, float]] = [
        (-40.0, -30.0, -15.0, 0.0),     # 南大西洋中部
        (-45.0, 20.0, -25.0, 40.0),     # 北大西洋中部
        (-170.0, -30.0, -120.0, 10.0),  # 太平洋中部
        (60.0, -35.0, 90.0, -10.0),     # 印度洋中部
    ]

    def is_land(self, lon: float, lat: float) -> bool | None:
        if any(a <= lon <= c and b <= lat <= d for a, b, c, d in self.OCEANS):
            return False
        if any(a <= lon <= c and b <= lat <= d for a, b, c, d in self.COUNTRIES.values()):
            return True
        return None

    def in_country(self, lon: float, lat: float, iso2: str, buffer_m: float = 1000.0) -> bool | None:
        box = self.COUNTRIES.get((iso2 or "").upper())
        if box is None:
            return None
        dlat = buffer_m / 111_320.0
        dlon = buffer_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
        a, b, c, d = box
        return (a - dlon) <= lon <= (c + dlon) and (b - dlat) <= lat <= (d + dlat)
