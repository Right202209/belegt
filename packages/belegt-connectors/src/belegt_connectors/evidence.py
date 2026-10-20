# Spec: 03 §3.5 Evidence（source_type、fact_key、extracted_value DEC-STR、locator）、04 §2.4 evidence-miner、
#       04 §2.1 EV-AUTO-ERP-ATTEST / EV-AUTO-0.90-LOC（此处只产出 candidate，不做晋升）
"""把连接器 envelope 中的一条记录转换成 03 `Evidence` 的业务字段（不含 id/version/信封），交给 evidence-miner 写图。

- `locator` 原样沿用记录的 locator（文件类：blob_sha256 + page/cell/bbox；ERP/SQL：`connector{connector_id, query_hash,
  record_key, fetched_at}`）。
- `source_type`：locator 有 `blob_sha256` 且有 page/cell/bbox → `document`；否则 → `erp`。
- 数字一律 DEC-STR；连接器不做换算、合计（交给 calc-service）。
- `status` 恒为 `candidate`；`verification` 不在这里给（晋升规则属于 guard / evidence-miner）。
"""
from __future__ import annotations

from typing import Any

from .spec import ConnectorError, dec_str


def evidence_source_type(locator: dict[str, Any]) -> str:
    if locator.get("blob_sha256") and any(k in locator for k in ("page", "cell", "bbox")):
        return "document"
    if locator.get("connector"):
        return "erp"
    raise ConnectorError("CONNECTOR_INPUT_INVALID", "record locator is not usable as Evidence locator")


def record_to_evidence(envelope: dict[str, Any], record: dict[str, Any], *, fact_key: str,
                       value_field: str | None = None, value_type: str = "number", unit: str | None = None,
                       boundary: str = "organization", datapoint_id: str | None = None,
                       confidence: float | None = None, keep_connector_locator: bool = True) -> dict[str, Any]:
    """envelope + record → Evidence 业务字段 dict。

    `value_field`：取值字段名；省略时依次尝试 `extracted_value`、`quantity`、`value`。
    """
    fields = record.get("fields") or {}
    if value_field is None:
        value_field = next((k for k in ("extracted_value", "quantity", "value") if k in fields), None)
    if value_field is None or value_field not in fields:
        raise ConnectorError("CONNECTOR_INPUT_INVALID", f"value field {value_field!r} not in record")
    raw = fields[value_field]
    locator = dict(record.get("locator") or {})
    st = evidence_source_type(locator)
    if st == "document" and not keep_connector_locator:
        locator.pop("connector", None)
    if value_type == "number":
        value: Any = dec_str(raw)
        unit = unit or record.get("unit") or fields.get("unit")
        if not unit:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "number evidence requires a unit")
    else:
        value = raw
    if confidence is None:
        if "confidence" in fields:
            confidence = float(fields["confidence"])
        else:
            confidence = 0.95 if envelope.get("attested") else 0.8
    ev: dict[str, Any] = {
        "source_type": st,
        "fact_key": fact_key,
        "extracted_value": value,
        "value_type": value_type,
        "boundary": boundary,
        "locator": locator,
        "confidence": round(float(confidence), 4),
        "status": "candidate",
        "tags": [f"connector:{envelope.get('connector_id')}", f"tool:{envelope.get('tool')}",
                 f"attested:{str(bool(envelope.get('attested'))).lower()}"],
    }
    if unit:
        ev["unit"] = unit
    if record.get("period"):
        ev["period"] = dict(record["period"])
    # 03 §3.5：valid_from 必填——事实期间起点；无期间时取抓取日
    ev["valid_from"] = (record.get("period") or {}).get("start") or str(envelope.get("fetched_at", ""))[:10]
    if datapoint_id:
        ev["datapoint_id"] = datapoint_id
    return ev
