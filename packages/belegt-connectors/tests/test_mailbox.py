from __future__ import annotations

import base64
import shutil
from pathlib import Path
from typing import Any

import pytest

from belegt_connectors import ConnectorError, LocalMailBackend, MailboxConnector, due_date_candidates, run_compliance
from belegt_connectors.samples import sample_messages
from belegt_connectors.spec import sha256_bytes


@pytest.mark.parametrize("text,exp", [
    ("Bitte füllen Sie den Fragebogen bis 15.01.2027 aus.", ["2027-01-15"]),
    ("Abgabetermin: 03.02.2027", ["2027-02-03"]),
    ("bis zum 15. Januar 2027", ["2027-01-15"]),
    ("please complete by January 31, 2027", ["2027-01-31"]),
    ("Deadline: 2027-03-01", ["2027-03-01"]),
    ("no later than 5th March 2027", ["2027-03-05"]),
    ("Bitte so schnell wie möglich antworten.", []),          # “尽快” → null（LBL-TRIAGE 边界）
    ("Rechnungsdatum 15.01.2027", []),                         # 无截止语义词
    ("bis 31.02.2027", []),                                    # 非法日期
])
def test_due_date_candidates(text: str, exp: list[str]) -> None:
    assert [d["date"] for d in due_date_candidates(text)] == exp


def test_mbox_list_get_attachments(sample_root: Path) -> None:
    c = MailboxConnector.from_path(sample_root / "mail" / "mailbox.mbox")
    assert [r["fields"]["folder"] for r in c.call("mail.imap.folders.list")["records"]] == ["INBOX"]
    env = c.call("mail.imap.messages.list", {"folder": "INBOX"})
    assert env["total"] == 5
    first = env["records"][0]["fields"]
    assert first["subject"].startswith("CO2-Fragebogen 2026")
    assert first["due_date_candidates"][0]["date"] == "2027-01-15"
    assert first["attachments"][0]["questionnaire_candidate"] is True
    assert first["external_data"] is True and first["date"] == "2026-10-01T09:15:00Z"
    reminder = env["records"][2]["fields"]
    assert reminder["is_reply"] is True and reminder["in_reply_to"] == "<req-2026-001@oem-autowerke.example>"
    full = c.call("mail.imap.messages.get", {"uid": 5})
    assert "IGNORE PREVIOUS INSTRUCTIONS" in full["records"][0]["fields"]["body_text"]  # 外部数据原样交给 guard
    assert full["records"][0]["locator"]["mime"] == "message/rfc822"
    att = c.call("mail.imap.attachments.get", {"uid": 1, "index": 0})["records"][0]
    data = base64.b64decode(att["fields"]["content_base64"])
    assert data.startswith(b"Nr.;Frage") and att["locator"]["blob_sha256"] == sha256_bytes(data)
    with pytest.raises(ConnectorError):
        c.call("mail.imap.attachments.get", {"uid": 4, "index": 0})


def test_filters_and_html_body(sample_root: Path) -> None:
    c = MailboxConnector.from_path(sample_root / "mail" / "maildir_eml")
    folders = {r["fields"]["folder"]: r["fields"]["messages"] for r in c.call("mail.imap.folders.list")["records"]}
    assert folders == {"Archiv": "1", "INBOX": "5"}
    env = c.call("mail.imap.messages.list", {"folder": "INBOX", "has_attachments": True})
    assert [r["fields"]["uid"] for r in env["records"]] == ["1", "2"]
    env = c.call("mail.imap.messages.list", {"folder": "INBOX", "from_contains": "oem-autowerke",
                                             "since": "2026-10-10"})
    assert [r["fields"]["uid"] for r in env["records"]] == ["3"]
    news = c.call("mail.imap.messages.get", {"folder": "INBOX", "uid": 4})["records"][0]["fields"]
    assert news["body_format"] == "plain" and "Energiepreise" in news["body_text"]
    arch = c.call("mail.imap.messages.list", {"folder": "Archiv"})["records"][0]["fields"]
    assert arch["attachments"][0]["format"] == "pdf"


def test_incremental_uid_sync(sample_root: Path, tmp_path: Path) -> None:
    d = tmp_path / "eml"
    shutil.copytree(sample_root / "mail" / "maildir_eml", d)
    c = MailboxConnector(LocalMailBackend(d))
    recs, token = c.sync("mail.imap.messages.list", {"folder": "INBOX"})
    assert len(recs) == 5
    _, msg = sample_messages()[1]
    (d / "INBOX" / "0099.eml").write_bytes(msg.as_bytes())
    c2 = MailboxConnector(LocalMailBackend(d))
    new, _ = c2.sync("mail.imap.messages.list", {"folder": "INBOX"}, token)
    assert [r["fields"]["uid"] for r in new] == ["6"]


class FakeIMAP:
    """最小 IMAP 假服务器：记录命令，断言只读（EXAMINE / BODY.PEEK，无 STORE/EXPUNGE）。"""

    def __init__(self, host: str, port: int) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.msgs = {101: sample_messages()[0][1].as_bytes(), 102: sample_messages()[1][1].as_bytes()}
        FakeIMAP.last = self

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", user, pw))
        return "OK", [b"ok"]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren) "/" INBOX', b'(\\Noselect) "/" "[Gmail]"', b'(\\HasNoChildren) "/" "Lieferanten Anfragen"']

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        assert readonly is True
        return "OK", [b"2"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        return code, [b"777"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in self.msgs)]
        if command == "FETCH":
            assert args[1] == "(BODY.PEEK[])"
            uid = int(args[0])
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.msgs[uid]), b")"]
        raise AssertionError(f"write command {command}")

    def store(self, *a: Any) -> None:  # pragma: no cover
        raise AssertionError("STORE must never be called")

    def logout(self) -> None:
        self.calls.append(("logout",))


def test_imap_backend_is_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BELEGT_SECRET_IMAP", "s3cr3t-pw")
    c = MailboxConnector.from_imap("imap.example", "intake@musterteile.example", "env:BELEGT_SECRET_IMAP",
                                   imap_factory=FakeIMAP, connector_id="conn:mailbox:intake")
    folders = [r["fields"]["folder"] for r in c.call("mail.imap.folders.list")["records"]]
    assert folders == ["INBOX", "Lieferanten Anfragen"]
    env = c.call("mail.imap.messages.list", {"folder": "INBOX"})
    assert [r["record_key"] for r in env["records"]] == ["INBOX:777:101", "INBOX:777:102"]
    fake = FakeIMAP.last
    assert ("login", "intake@musterteile.example", "s3cr3t-pw") in fake.calls
    assert all(cmd[0] != "uid" or cmd[1] in ("SEARCH", "FETCH") for cmd in fake.calls)
    assert any(cmd[0] == "select" and cmd[1] == '"Lieferanten Anfragen"' for cmd in fake.calls)
    rep = run_compliance(c, secrets=["s3cr3t-pw"])
    assert rep.ok, rep.to_text()


def test_mailbox_compliance(sample_root: Path) -> None:
    for p in (sample_root / "mail" / "mailbox.mbox", sample_root / "mail" / "maildir_eml"):
        rep = run_compliance(MailboxConnector.from_path(p))
        assert rep.ok, rep.to_text()
