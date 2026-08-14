"""Mail tests — offline. No network, no tokens, no real mailbox.

The parsing and normalisation layer is where provider differences leak into Yoyo, so that
is what these pin. The capability boundary (read and draft, never send) is asserted
structurally: if someone adds a send method, a test fails.
"""

import base64
from pathlib import Path

import pytest

from yoyo import mail
from yoyo.mail import base, gmail, graph


# ------------------------------------------------- capability boundary ----


@pytest.mark.parametrize("provider_cls", [gmail.GmailProvider, graph.GraphProvider])
def test_no_provider_can_send(provider_cls):
    """Yoyo drafts; a human sends. Adding a send path must break a test, not slip in."""
    forbidden = [n for n in dir(provider_cls) if "send" in n.lower()]
    assert forbidden == [], f"{provider_cls.__name__} grew a send path: {forbidden}"


def test_scopes_do_not_include_send():
    assert not any("send" in s.lower() for s in gmail.SCOPES)
    assert not any("send" in s.lower() for s in graph.SCOPES)


def test_graph_scopes_are_minimal():
    assert set(graph.SCOPES) == {"Mail.Read", "Mail.ReadWrite"}


# ------------------------------------------------------------ config ----


def _cfg(tmp_path, body: str) -> Path:
    p = tmp_path / "yoyo-mail.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_config_is_not_an_error(tmp_path):
    assert mail.load_accounts(tmp_path / "absent.yaml") == []


def test_accounts_parse(tmp_path):
    p = _cfg(
        tmp_path,
        "accounts:\n"
        "  personal:\n    provider: gmail\n    enabled: true\n"
        "  work:\n    provider: microsoft\n    client_id: abc-123\n    tenant: contoso\n",
    )
    specs = {s.name: s for s in mail.load_accounts(p)}
    assert specs["personal"].provider == "gmail"
    assert specs["work"].client_id == "abc-123"
    assert specs["work"].tenant == "contoso"


def test_unknown_provider_is_rejected(tmp_path):
    p = _cfg(tmp_path, "accounts:\n  x:\n    provider: carrier-pigeon\n")
    with pytest.raises(ValueError, match="must be 'gmail' or 'microsoft'"):
        mail.load_accounts(p)


def test_microsoft_without_client_id_fails_clearly(tmp_path):
    p = _cfg(tmp_path, "accounts:\n  work:\n    provider: microsoft\n")
    spec = mail.load_accounts(p)[0]
    with pytest.raises(mail.MailError, match="client_id"):
        mail.build(spec)


def test_resolve_requires_a_name_when_several_accounts_exist(tmp_path):
    p = _cfg(
        tmp_path,
        "accounts:\n"
        "  a:\n    provider: gmail\n"
        "  b:\n    provider: microsoft\n    client_id: x\n",
    )
    with pytest.raises(mail.MailError, match="name one explicitly"):
        mail.resolve(None, p)


def test_resolve_is_implicit_with_one_account(tmp_path):
    p = _cfg(tmp_path, "accounts:\n  only:\n    provider: gmail\n")
    assert mail.resolve(None, p).account == "only"


def test_unknown_account_lists_the_configured_ones(tmp_path):
    p = _cfg(tmp_path, "accounts:\n  only:\n    provider: gmail\n")
    with pytest.raises(mail.MailError, match="Configured: only"):
        mail.resolve("nope", p)


def test_disabled_accounts_are_not_resolvable(tmp_path):
    p = _cfg(tmp_path, "accounts:\n  off_one:\n    provider: gmail\n    enabled: false\n")
    with pytest.raises(mail.MailError, match="No mail accounts configured"):
        mail.resolve(None, p)


def test_shipped_config_ships_disabled():
    """Nothing should try to authenticate until the user has set it up."""
    for spec in mail.load_accounts():
        assert spec.enabled is False, f"{spec.name} ships enabled"


# ------------------------------------------------------------- gmail ----


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


GMAIL_MSG = {
    "id": "m1",
    "threadId": "t1",
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "Quick question about the invoice",
    "internalDate": "1755100000000",
    "payload": {
        "mimeType": "multipart/alternative",
        "headers": [
            {"name": "Subject", "value": "Invoice 4417"},
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "To", "value": "bob@example.com, carol@example.com"},
            {"name": "Date", "value": "Thu, 14 Aug 2026 09:00:00 +0000"},
        ],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Plain body wins.")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML body</p>")}},
        ],
    },
}


def test_gmail_parses_headers_and_recipients():
    m = gmail._parse_message(GMAIL_MSG, "personal", include_body=True)
    assert m.subject == "Invoice 4417"
    assert m.sender == "Alice <alice@example.com>"
    assert m.to == ["bob@example.com", "carol@example.com"]
    assert m.unread is True
    assert m.thread_id == "t1"


def test_gmail_prefers_plain_text_over_html():
    m = gmail._parse_message(GMAIL_MSG, "personal", include_body=True)
    assert m.body == "Plain body wins."


def test_gmail_falls_back_to_html_when_no_plain_part():
    data = {
        "id": "m2",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "S"}],
            "body": {"data": _b64("<p>Only <b>HTML</b> here</p>")},
        },
    }
    m = gmail._parse_message(data, "personal", include_body=True)
    assert "Only HTML here" in m.body


def test_gmail_body_omitted_when_not_requested():
    assert gmail._parse_message(GMAIL_MSG, "personal", include_body=False).body is None


def test_gmail_handles_a_missing_date():
    data = {"id": "m3", "payload": {"headers": []}}
    assert gmail._parse_message(data, "a", include_body=False).date is None


def test_gmail_draft_mime_round_trips():
    raw = gmail.build_mime(["x@y.com"], "Subject line", "Body text", cc=["z@y.com"])
    decoded = base64.urlsafe_b64decode(gmail.b64url(raw)).decode()
    assert "To: x@y.com" in decoded
    assert "Cc: z@y.com" in decoded
    assert "Subject: Subject line" in decoded
    assert "Body text" in decoded


def test_gmail_auth_required_without_a_token(tmp_path):
    p = gmail.GmailProvider(
        account="a",
        client_secrets=tmp_path / "secrets.json",
        token_path=tmp_path / "token.json",
    )
    assert p.is_authenticated() is False


def test_gmail_missing_client_secrets_explains_the_fix(tmp_path):
    p = gmail.GmailProvider(
        account="a",
        client_secrets=tmp_path / "nope.json",
        token_path=tmp_path / "t.json",
    )
    with pytest.raises(base.MailError, match="Desktop app"):
        p.authenticate()


# ------------------------------------------------------------- graph ----

GRAPH_MSG = {
    "id": "g1",
    "conversationId": "c1",
    "subject": "Quarterly review",
    "from": {"emailAddress": {"name": "Dana", "address": "dana@contoso.com"}},
    "toRecipients": [{"emailAddress": {"address": "me@contoso.com"}}],
    "ccRecipients": [],
    "receivedDateTime": "2026-08-14T09:30:00Z",
    "bodyPreview": "Numbers attached",
    "isRead": False,
    "body": {"contentType": "html", "content": "<p>Numbers <b>attached</b></p>"},
}


def test_graph_parses_addresses_and_flags():
    m = graph._parse(GRAPH_MSG, "work", include_body=True)
    assert m.subject == "Quarterly review"
    assert m.sender == "Dana <dana@contoso.com>"
    assert m.to == ["me@contoso.com"]
    assert m.unread is True
    assert m.thread_id == "c1"


def test_graph_converts_html_bodies_to_text():
    m = graph._parse(GRAPH_MSG, "work", include_body=True)
    assert m.body == "Numbers attached"


def test_graph_parses_the_timestamp():
    m = graph._parse(GRAPH_MSG, "work", include_body=False)
    assert m.date.year == 2026 and m.date.month == 8


def test_graph_survives_missing_optional_fields():
    m = graph._parse({"id": "x"}, "work", include_body=True)
    assert m.subject == "" and m.sender == "" and m.to == []


# ----------------------------------------------------- shared helpers ----


def test_both_providers_produce_the_same_shape():
    """The point of the adapter layer: one shape, whatever the source."""
    a = gmail._parse_message(GMAIL_MSG, "personal", include_body=True).as_dict(True)
    b = graph._parse(GRAPH_MSG, "work", include_body=True).as_dict(True)
    assert a.keys() == b.keys()


def test_long_bodies_are_truncated_with_a_marker():
    out = base.truncate("x" * 25_000, limit=20_000)
    assert len(out) < 25_000
    assert "truncated" in out


def test_html_stripping_drops_scripts_and_styles():
    html = "<style>p{color:red}</style><script>alert(1)</script><p>Real content</p>"
    text = base.html_to_text(html)
    assert "Real content" in text
    assert "alert" not in text and "color" not in text
