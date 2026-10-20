# Spec: 08 §6.5（自托管模式）、10（运维配置）
"""配置：全部来自环境变量（12-factor），便于单容器自托管。"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass
class Settings:
    db_url: str = "sqlite:///./data/supplier.db"
    data_dir: Path = Path("./data")
    public_url: str = "http://localhost:8080"
    instance_secret: bytes = b""          # 魔法链接签名、CSRF、提交包 JWS
    manifest_secret: bytes = b""          # 校验客户侧推送的请求清单（JWS HS256）
    webhook_secret: bytes = b""           # 与客户侧互发 Webhook 的 HMAC 密钥
    admin_key: str | None = None          # /supplier-api/v1/admin/*（租户侧/运维）
    core_url: str | None = None           # 客户侧根地址（/supplier-inbound/v1/*）
    outbox_dir: Path | None = None        # 开发：邮件写入 .eml
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    mail_from: str = "no-reply@belegt.local"
    internal_domains: list[str] = field(default_factory=list)
    extra_link_domains: list[str] = field(default_factory=list)
    device_binding: bool = True
    default_locale: str = "en"
    max_upload_bytes: int = 25 * 1024 * 1024
    max_request_upload_bytes: int = 200 * 1024 * 1024
    trust_proxy_headers: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("BELEGT_SUPPLIER_DATA_DIR", "./data") or "./data")
        def secret(name: str) -> bytes:
            v = _env(name)
            return v.encode("utf-8") if v else b""
        s = cls(
            db_url=_env("BELEGT_SUPPLIER_DB_URL") or f"sqlite:///{(data_dir / 'supplier.db').as_posix()}",
            data_dir=data_dir,
            public_url=(_env("BELEGT_SUPPLIER_PUBLIC_URL", "http://localhost:8080") or "").rstrip("/"),
            instance_secret=secret("BELEGT_SUPPLIER_SECRET"),
            manifest_secret=secret("BELEGT_SUPPLIER_MANIFEST_SECRET"),
            webhook_secret=secret("BELEGT_SUPPLIER_WEBHOOK_SECRET"),
            admin_key=_env("BELEGT_SUPPLIER_ADMIN_KEY"),
            core_url=_env("BELEGT_SUPPLIER_CORE_URL"),
            outbox_dir=Path(_env("BELEGT_SUPPLIER_OUTBOX_DIR")) if _env("BELEGT_SUPPLIER_OUTBOX_DIR") else None,
            smtp_host=_env("BELEGT_SUPPLIER_SMTP_HOST"),
            smtp_port=int(_env("BELEGT_SUPPLIER_SMTP_PORT", "587") or 587),
            smtp_user=_env("BELEGT_SUPPLIER_SMTP_USER"),
            smtp_password=_env("BELEGT_SUPPLIER_SMTP_PASSWORD"),
            smtp_starttls=(_env("BELEGT_SUPPLIER_SMTP_STARTTLS", "true") or "true").lower() != "false",
            mail_from=_env("BELEGT_SUPPLIER_MAIL_FROM", "no-reply@belegt.local") or "no-reply@belegt.local",
            internal_domains=[d for d in (_env("BELEGT_SUPPLIER_INTERNAL_DOMAINS", "") or "").split(",") if d],
            extra_link_domains=[d for d in (_env("BELEGT_SUPPLIER_LINK_DOMAINS", "") or "").split(",") if d],
            device_binding=(_env("BELEGT_SUPPLIER_DEVICE_BINDING", "true") or "true").lower() != "false",
            default_locale=_env("BELEGT_SUPPLIER_DEFAULT_LOCALE", "en") or "en",
            trust_proxy_headers=(_env("BELEGT_SUPPLIER_TRUST_PROXY", "false") or "false").lower() == "true",
        )
        return s.ensure_secrets()

    def ensure_secrets(self) -> "Settings":
        """未配置密钥时为本进程生成随机密钥（重启后旧链接失效）；生产必须显式配置。"""
        if not self.instance_secret:
            self.instance_secret = secrets.token_bytes(32)
        if not self.manifest_secret:
            self.manifest_secret = self.webhook_secret or secrets.token_bytes(32)
        if not self.webhook_secret:
            self.webhook_secret = secrets.token_bytes(32)
        return self

    @property
    def portal_host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.public_url).hostname or "localhost"
