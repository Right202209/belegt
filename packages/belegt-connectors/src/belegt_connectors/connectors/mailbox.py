# Spec: FR-101（接入邮箱、识别 Excel/PDF/Word 问卷、提取截止日期）；04 §2.2 intake-triage 触发（IMAP）、§2.0 外部文本隔离；
#       04 §6.1 只读连接器（EXAMINE + BODY.PEEK，不改 \Seen 标志）
"""IMAP 收件连接器（只读）。后端可替换：

- `ImapBackend`：真实 IMAP（imaplib，SSL）。`EXAMINE`（只读选择）+ `UID FETCH (BODY.PEEK[])`，从不设置 `\\Seen`、
  从不 STORE/EXPUNGE/COPY/APPEND。凭据经 `secret_ref` 解析。
- `LocalMailBackend`：本地 `.mbox` 文件或 `.eml` 目录（每个子目录一个文件夹），用于测试与离线演示。

工具：
- `mail.imap.folders.list`
- `mail.imap.messages.list`：头信息、附件清单（含问卷候选判定）、截止日期候选（确定性提示，不是结论）；按 UID 增量同步
- `mail.imap.messages.get`：正文（纯文本优先，HTML 去标签）——**外部数据**，`external_data=true`，调用方须放入 `<external_data>` 块
- `mail.imap.attachments.get`：附件原件（base64，≤ 10 MB）+ `blob_sha256`，供 belegt-io 解析与内容寻址存储
"""
from __future__ import annotations

import base64
import imaplib
import mailbox as std_mailbox
import re
import zlib
from datetime import date, datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Protocol

from ..base import BaseConnector, CallContext
from ..spec import CURSOR_PROPS, SYNC_PROPS, AuthSpec, ConnectorError, object_schema, sha256_bytes

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
PREVIEW_CHARS = 280

QUESTIONNAIRE_EXT = {".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls", ".csv": "csv", ".pdf": "pdf",
                     ".docx": "docx", ".doc": "doc"}
_Q_WORDS = re.compile(r"(frage(n)?bogen|questionnaire|survey|self[- ]?assessment|abfrage|datenanfrage|"
                      r"data request|saq|cmrt|cbam|nachhaltigkeit|sustainability|co2|esg)", re.I)

MONTHS_DE = {"januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
             "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
MONTHS_EN = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
             "september": 9, "october": 10, "november": 11, "december": 12}
_DUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:bis(?:\s+(?:zum|spätestens))?|spätestens(?:\s+am)?|frist[:\s]+|deadline[:\s]+|"
                r"abgabe(?:termin)?[:\s]+)\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I), "dmy"),
    (re.compile(r"\b(?:bis(?:\s+zum)?|spätestens(?:\s+am)?|frist[:\s]+)\s*(\d{1,2})\.\s*([A-Za-zäÄ]+)\s+(\d{4})",
                re.I), "d_month_de"),
    (re.compile(r"\b(?:by|before|due(?:\s+date)?[:\s]+|deadline[:\s]+|no later than)\s*"
                r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I), "month_d_en"),
    (re.compile(r"\b(?:by|before|due(?:\s+date)?[:\s]+|deadline[:\s]+|no later than)\s*"
                r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", re.I), "d_month_en"),
    (re.compile(r"\b(?:by|before|due(?:\s+date)?[:\s]+|deadline[:\s]+|bis|frist[:\s]+)\s*(\d{4})-(\d{2})-(\d{2})",
                re.I), "iso"),
]


def due_date_candidates(text: str) -> list[dict[str, str]]:
    """确定性截止日期候选（FR-101 提示）。只认“bis/by/deadline …”附近的显式日期；不推断“尽快”。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for rx, kind in _DUE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "dmy":
                    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                elif kind == "d_month_de":
                    mon = MONTHS_DE.get(m.group(2).lower())
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(1)))
                elif kind == "month_d_en":
                    mon = MONTHS_EN.get(m.group(1).lower())
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(2)))
                elif kind == "d_month_en":
                    mon = MONTHS_EN.get(m.group(2).lower())
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(1)))
                else:
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            iso = d.isoformat()
            if iso not in seen:
                seen.add(iso)
                out.append({"date": iso, "evidence": m.group(0).strip()[:80]})
    return out


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("p", "br", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", "".join(p.parts)).strip()


# —— 后端 ——

class MailBackend(Protocol):
    def folders(self) -> list[str]: ...
    def uidvalidity(self, folder: str) -> int: ...
    def uids(self, folder: str) -> list[int]: ...
    def fetch(self, folder: str, uid: int) -> bytes: ...


class LocalMailBackend:
    """`.mbox` 文件 → 单文件夹 INBOX；目录 → 根下 `.eml` 为 INBOX，子目录各为一个文件夹，`*.mbox` 以文件名为文件夹。

    UID = 文件夹内按文件名（mbox 按出现顺序）排序后的 1 起序号；UIDVALIDITY = 文件夹路径的 CRC32。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"mail store {path!s} not found")
        self._cache: dict[str, list[bytes]] = {}

    def _load(self) -> dict[str, list[bytes]]:
        if self._cache:
            return self._cache
        store: dict[str, list[bytes]] = {}
        if self.path.is_file():
            store["INBOX"] = self._mbox(self.path)
        else:
            root_eml = sorted(self.path.glob("*.eml"))
            if root_eml:
                store["INBOX"] = [p.read_bytes() for p in root_eml]
            for sub in sorted(p for p in self.path.iterdir() if p.is_dir()):
                emls = sorted(sub.rglob("*.eml"))
                if emls:
                    store[sub.name] = [p.read_bytes() for p in emls]
            for mb in sorted(self.path.glob("*.mbox")):
                store[mb.stem] = self._mbox(mb)
        self._cache = store
        return store

    @staticmethod
    def _mbox(p: Path) -> list[bytes]:
        box = std_mailbox.mbox(str(p), create=False)
        try:
            return [box.get_bytes(k) for k in box.keys()]
        finally:
            box.close()

    def folders(self) -> list[str]:
        return list(self._load())

    def uidvalidity(self, folder: str) -> int:
        self._check(folder)
        return zlib.crc32(f"{self.path.resolve()}::{folder}".encode()) or 1

    def uids(self, folder: str) -> list[int]:
        return list(range(1, len(self._check(folder)) + 1))

    def fetch(self, folder: str, uid: int) -> bytes:
        msgs = self._check(folder)
        if not 1 <= uid <= len(msgs):
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"uid {uid} not in {folder}")
        return msgs[uid - 1]

    def _check(self, folder: str) -> list[bytes]:
        store = self._load()
        if folder not in store:
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"folder {folder!r} not found")
        return store[folder]


class ImapBackend:
    """真实 IMAP（只读）。`imap_factory` 可注入（测试用假服务器）。"""

    def __init__(self, host: str, username: str, password_resolver: Callable[[], str], *, port: int = 993,
                 ssl: bool = True, imap_factory: Callable[..., Any] | None = None) -> None:
        self.host, self.port, self.username, self.ssl = host, port, username, ssl
        self._pw = password_resolver
        self._factory = imap_factory or (imaplib.IMAP4_SSL if ssl else imaplib.IMAP4)
        self._conn: Any = None
        self._selected: str | None = None
        self._uidvalidity: dict[str, int] = {}

    def _c(self) -> Any:
        if self._conn is None:
            conn = self._factory(self.host, self.port)
            conn.login(self.username, self._pw())
            self._conn = conn
        return self._conn

    def _examine(self, folder: str) -> None:
        if self._selected == folder:
            return
        typ, _ = self._c().select(self._quote(folder), readonly=True)  # readonly=True → EXAMINE
        if typ != "OK":
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"folder {folder!r} not found")
        typ, data = self._c().response("UIDVALIDITY")
        if data and data[0]:
            self._uidvalidity[folder] = int(data[0])
        self._selected = folder

    @staticmethod
    def _quote(folder: str) -> str:
        return folder if re.fullmatch(r"[A-Za-z0-9_.\-/]+", folder) else '"' + folder.replace('"', '\\"') + '"'

    def folders(self) -> list[str]:
        typ, data = self._c().list()
        if typ != "OK":
            raise ConnectorError("CONNECTOR_SOURCE_ERROR", "LIST failed")
        out = []
        for raw in data or []:
            line = raw.decode() if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$', line)
            if not m or "\\Noselect" in m.group("flags"):
                continue
            out.append(m.group("name").strip().strip('"'))
        return out

    def uidvalidity(self, folder: str) -> int:
        self._examine(folder)
        return self._uidvalidity.get(folder, 1)

    def uids(self, folder: str) -> list[int]:
        self._examine(folder)
        typ, data = self._c().uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise ConnectorError("CONNECTOR_SOURCE_ERROR", "UID SEARCH failed")
        return sorted(int(x) for x in (data[0] or b"").split())

    def fetch(self, folder: str, uid: int) -> bytes:
        self._examine(folder)
        typ, data = self._c().uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK：不设置 \Seen
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"uid {uid} not in {folder}")
        return data[0][1]

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            finally:
                self._conn = None


# —— 连接器 ——

def _addr_list(v: Any) -> list[str]:
    return [f"{n} <{a}>" if n else a for n, a in getaddresses([str(v)])] if v else []


def _parse(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[return-value]


def _body_text(msg: EmailMessage) -> tuple[str, str]:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return "", "none"
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):  # pragma: no cover
        content = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        return html_to_text(content), "html"
    return content.strip(), "plain"


def _attachments(msg: EmailMessage) -> list[tuple[dict[str, Any], bytes]]:
    out = []
    for i, part in enumerate(msg.iter_attachments()):
        data = part.get_payload(decode=True) or b""
        fname = part.get_filename() or f"attachment-{i}"
        ext = Path(fname).suffix.lower()
        kind = QUESTIONNAIRE_EXT.get(ext)
        meta = {
            "index": i, "filename": fname, "mime": part.get_content_type(), "size": len(data),
            "blob_sha256": sha256_bytes(data), "format": kind,
            # 问卷候选：格式可解析，且文件名或邮件主题含问卷/数据请求关键词（确定性提示）
            "questionnaire_candidate": bool(kind) and bool(_Q_WORDS.search(fname)),
        }
        out.append((meta, data))
    return out


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class MailboxConnector(BaseConnector):
    kind = "mailbox"
    category = "mail"
    system = "imap"
    title = "IMAP mailbox (read-only intake)"
    source_system = "imap"
    attested = False

    def __init__(self, backend: MailBackend, connector_id: str | None = None, **kw: Any) -> None:
        if isinstance(backend, ImapBackend):
            kw.setdefault("auth", AuthSpec(mode="basic", secret_ref=None, notes="IMAP 登录凭据经 secret_ref 解析"))
        else:
            kw.setdefault("auth", AuthSpec(mode="local_file"))
        super().__init__(connector_id or "conn:mailbox:default", **kw)
        self.backend = backend
        self.register("mail.imap.folders.list", "列出可读文件夹", object_schema({}), self._folders)
        self.register(
            "mail.imap.messages.list",
            "列出邮件头与附件清单；附问卷候选判定与截止日期候选（确定性提示，由 intake-triage 定夺）。按 UID 增量。",
            object_schema({"folder": {"type": "string", "default": "INBOX"},
                           "since": {"type": "string", "format": "date"},
                           "before": {"type": "string", "format": "date"},
                           "from_contains": {"type": "string"}, "subject_contains": {"type": "string"},
                           "has_attachments": {"type": "boolean"}, **CURSOR_PROPS, **SYNC_PROPS}),
            self._list, paginated=True, incremental=True)
        self.register(
            "mail.imap.messages.get", "读取单封邮件正文（外部数据，external_data=true）与附件清单",
            object_schema({"folder": {"type": "string", "default": "INBOX"}, "uid": {"type": "integer", "minimum": 1},
                           "max_chars": {"type": "integer", "minimum": 100, "maximum": 200000, "default": 20000}},
                          ["uid"]),
            self._get)
        self.register(
            "mail.imap.attachments.get", "读取附件原件（base64，≤ 10 MB）与 blob_sha256",
            object_schema({"folder": {"type": "string", "default": "INBOX"}, "uid": {"type": "integer", "minimum": 1},
                           "index": {"type": "integer", "minimum": 0}}, ["uid", "index"]),
            self._attachment)

    @classmethod
    def from_path(cls, path: str | Path, **kw: Any) -> "MailboxConnector":
        return cls(LocalMailBackend(path), **kw)

    @classmethod
    def from_imap(cls, host: str, username: str, secret_ref: str, *, port: int = 993, ssl: bool = True,
                  imap_factory: Callable[..., Any] | None = None, **kw: Any) -> "MailboxConnector":
        holder: dict[str, MailboxConnector] = {}
        backend = ImapBackend(host, username, lambda: holder["c"].secrets.resolve(secret_ref), port=port, ssl=ssl,
                              imap_factory=imap_factory)
        kw.setdefault("auth", AuthSpec(mode="basic", secret_ref=secret_ref, notes="IMAP 登录；EXAMINE + BODY.PEEK"))
        c = cls(backend, **kw)
        holder["c"] = c
        return c

    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        folder = self.backend.folders()[0] if self.backend.folders() else "INBOX"
        probes: dict[str, dict[str, Any]] = {"mail.imap.folders.list": {}, "mail.imap.messages.list": {"folder": folder}}
        uids = self.backend.uids(folder)
        if uids:
            probes["mail.imap.messages.get"] = {"folder": folder, "uid": uids[0]}
            for uid in uids:
                if _attachments(_parse(self.backend.fetch(folder, uid))):
                    probes["mail.imap.attachments.get"] = {"folder": folder, "uid": uid, "index": 0}
                    break
        return probes

    # —— handlers ——
    def _key(self, folder: str, uid: int) -> str:
        return f"{folder}:{self.backend.uidvalidity(folder)}:{uid}"

    def _folders(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        out = []
        for f in self.backend.folders():
            out.append({"record_key": f, "fields": {"folder": f, "uidvalidity": self.backend.uidvalidity(f),
                                                    "messages": len(self.backend.uids(f))}})
        return out

    def _summary(self, folder: str, uid: int, raw: bytes) -> dict[str, Any]:
        msg = _parse(raw)
        body, kind = _body_text(msg)
        atts = [m for m, _ in _attachments(msg)]
        subject = str(msg.get("Subject", ""))
        for a in atts:  # 主题含问卷关键词时，可解析附件均为候选
            a["questionnaire_candidate"] = a["questionnaire_candidate"] or (bool(a["format"]) and
                                                                            bool(_Q_WORDS.search(subject)))
        try:
            dt = _utc(parsedate_to_datetime(str(msg["Date"]))) if msg["Date"] else None
        except (TypeError, ValueError):
            dt = None
        fields = {
            "folder": folder, "uid": uid, "message_id": str(msg.get("Message-ID", "")) or None,
            "in_reply_to": str(msg.get("In-Reply-To", "")) or None,
            "from": _addr_list(msg.get("From")), "to": _addr_list(msg.get("To")), "cc": _addr_list(msg.get("Cc")),
            "subject": subject, "date": dt, "has_attachments": bool(atts), "attachments": atts,
            "is_reply": bool(msg.get("In-Reply-To")) or bool(re.match(r"^\s*(re|aw|wg|fw|fwd)\s*:", subject, re.I)),
            "body_preview": body[:PREVIEW_CHARS], "body_format": kind,
            "due_date_candidates": due_date_candidates(subject + "\n" + body),
            "external_data": True,
        }
        return {"record_key": self._key(folder, uid), "fields": fields,
                "locator": {"blob_sha256": sha256_bytes(raw), "mime": "message/rfc822"}, "_wm": uid}

    def _list(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        folder = args.get("folder") or "INBOX"
        out = []
        for uid in self.backend.uids(folder):
            if ctx.since is not None and uid <= int(ctx.since):
                continue  # 增量：UID 单调递增，跳过已同步部分（不 fetch）
            rec = self._summary(folder, uid, self.backend.fetch(folder, uid))
            f = rec["fields"]
            d = f["date"].date().isoformat() if f["date"] else None
            if args.get("since") and (d is None or d < args["since"]):
                continue
            if args.get("before") and (d is None or d >= args["before"]):
                continue
            if args.get("from_contains") and not any(args["from_contains"].lower() in a.lower() for a in f["from"]):
                continue
            if args.get("subject_contains") and args["subject_contains"].lower() not in f["subject"].lower():
                continue
            if "has_attachments" in args and bool(args["has_attachments"]) != f["has_attachments"]:
                continue
            out.append(rec)
        return out

    def _get(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        folder, uid = args.get("folder") or "INBOX", int(args["uid"])
        raw = self.backend.fetch(folder, uid)
        rec = self._summary(folder, uid, raw)
        body, _ = _body_text(_parse(raw))
        limit = int(args.get("max_chars") or 20000)
        rec["fields"]["body_text"] = body[:limit]
        rec["fields"]["body_truncated"] = len(body) > limit
        rec.pop("_wm", None)
        return [rec]

    def _attachment(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        folder, uid, idx = args.get("folder") or "INBOX", int(args["uid"]), int(args["index"])
        atts = _attachments(_parse(self.backend.fetch(folder, uid)))
        if idx >= len(atts):
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"attachment {idx} not found")
        meta, data = atts[idx]
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ConnectorError("CONNECTOR_LIMIT", "attachment exceeds 10 MB")
        return [{"record_key": f"{self._key(folder, uid)}#att{idx}",
                 "fields": {**meta, "content_base64": base64.b64encode(data).decode()},
                 "locator": {"blob_sha256": meta["blob_sha256"], "mime": meta["mime"]}}]
