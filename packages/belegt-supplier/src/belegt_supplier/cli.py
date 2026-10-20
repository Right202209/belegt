# Spec: 08 §6.2（自托管：单容器）、FR-309（模板审校流水线）
"""命令行：`belegt-supplier serve | templates list|lint|review|render | demo | chase-tick`。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta


def _cmd_serve(a) -> int:
    import uvicorn

    from .config import Settings
    from .web.app import create_app

    settings = Settings.from_env()
    app = create_app(settings)
    # access_log 关闭：URL 中含魔法链接 token，不得进入日志（08 §7.1）
    uvicorn.run(app, host=a.host, port=a.port, access_log=False, proxy_headers=settings.trust_proxy_headers)
    return 0


def _registry():
    from .config import Settings
    from .service import SupplierService

    return SupplierService(Settings.from_env())


def _cmd_templates(a) -> int:
    from .messages import TemplateRegistry

    if a.action == "list":
        reg = TemplateRegistry.load_builtin() if a.no_db else _registry().registry
        for t in reg.all():
            print(f"{t.id:52} {reg.effective_status(t.id):10} {','.join(t.variants):28} {reg.reviewed_by(t.id) or '-'}")
        return 0
    if a.action == "lint":
        reg = TemplateRegistry.load_builtin()
        issues = reg.lint()
        for i in issues:
            print(f"{i.severity.upper():7} {i.template_id} {i.code}: {i.message}")
        return 1 if any(i.severity == "error" for i in issues) else 0
    if a.action == "review":
        if not a.id or not a.by:
            print("usage: belegt-supplier templates review <template_id> --by <reviewer>", file=sys.stderr)
            return 2
        svc = _registry()
        svc.review_template(a.id, a.by)
        print(f"{a.id} reviewed by {a.by}")
        return 0
    if a.action == "render":
        reg = TemplateRegistry.load_builtin()
        ctx = json.loads(open(a.ctx, encoding="utf-8").read()) if a.ctx else _demo_ctx()
        m = reg.render(a.id, ctx, variant=a.variant)
        print("Subject:", m.subject)
        print()
        print(m.body_text)
        return 0
    return 2


def _demo_ctx() -> dict:
    return {
        "supplier_name": "Anadolu Çelik A.Ş.", "contact_name": "Ayşe Yılmaz", "customer_name": "Müller Stanztechnik GmbH",
        "products": [{"name": "Warmband", "cn_code": "72083900", "net_mass": "1200"}],
        "datapoints": ["dp:prod.cbam.installation_id", "dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect"],
        "period": {"start": "2026-01-01", "end": "2026-12-31"}, "due_date": "2027-03-15",
        "link_expires_at": "2027-03-29", "portal_link": "https://supplier.example.org/r/slk_demo",
        "sender_name": "Anna Weber", "sender_role": "Einkauf", "sreq_id": "sreq_01JD8Y3K5M2N4P6Q8R0S2T4V6W",
        "privacy_note_link": "https://mueller.example.org/privacy", "reminder_count": 3,
        "calc": {"result": "12 300", "low": "9 800", "high": "14 900", "ref": "calc_01JD8Y3K5M2N4P6Q8R0S2T4V6W#v1"},
        "evidence_label": "ISO 14064 verification report", "valid_until": "2027-01-31",
    }


def _cmd_demo(a) -> int:
    """创建一个演示请求并打印门户链接（开发用，邮件写入 outbox）。"""
    from .config import Settings
    from .ids import new_id
    from .models import RequestManifest
    from .service import SupplierService

    svc = SupplierService(Settings.from_env())
    m = RequestManifest(
        sreq_ref=f"{new_id('sreq')}#v1", org_id=new_id("org"), counterparty_id=new_id("cp"),
        customer_name="Müller Stanztechnik GmbH", supplier_name="Anadolu Çelik A.Ş.",
        contact_email="ayse@anadolu-celik.example", contact_name="Ayşe Yılmaz", locale=a.locale,
        datapoints=["dp:prod.cbam.installation_id", "dp:prod.cbam.production_route", "dp:prod.cbam.see_direct",
                    "dp:prod.cbam.see_indirect", "dp:prod.cbam.specific_electricity_consumption"],
        products=[{"product_id": new_id("prod"), "name": "Warmband", "cn_code": "72083900"},
                  {"product_id": new_id("prod"), "name": "Edelstahlblech", "cn_code": "72193400"}],
        period={"start": "2026-01-01", "end": "2026-12-31"}, due_date=date.today() + timedelta(days=21),
        supplier_timezone="Europe/Istanbul", supplier_country="TR",
        reference={"cbam_defaults": {"72083900": "1.98", "72193400": "2.40"}, "grid_factors": {"TR": "0.47"}},
    )
    res = svc.ingest_manifest(m, status="sent", approval_ref=f"{new_id('apr')}#v1")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def _cmd_tick(a) -> int:
    svc = _registry()
    for r in svc.chase_tick():
        print(json.dumps(r.__dict__, ensure_ascii=False, default=str))
    print(json.dumps(svc.deliver_webhooks()))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="belegt-supplier")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the supplier portal + API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=_cmd_serve)
    t = sub.add_parser("templates", help="message templates (stpl:)")
    t.add_argument("action", choices=["list", "lint", "review", "render"])
    t.add_argument("id", nargs="?")
    t.add_argument("--by")
    t.add_argument("--variant")
    t.add_argument("--ctx", help="JSON file with render context")
    t.add_argument("--no-db", action="store_true", help="list file status only")
    t.set_defaults(fn=_cmd_templates)
    d = sub.add_parser("demo", help="create a demo request and print its portal link")
    d.add_argument("--locale", default="de")
    d.set_defaults(fn=_cmd_demo)
    c = sub.add_parser("chase-tick", help="run one chase-policy tick and deliver webhooks")
    c.set_defaults(fn=_cmd_tick)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
