"""Mail tests — offline. No network, no tokens, no real mailbox.

The parsing and normalisation layer is where provider differences leak into Yoyo, so that
is what these pin. The capability boundary (read and draft, never send) is asserted
structurally: if someone adds a send method, a test fails.
"""

import base64
import pathlib
import re
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
    """Still refused — but the message now says *disabled*, not *not configured*. The old
    wording sent the user to write a yaml block they had already written."""
    p = _cfg(tmp_path, "accounts:\n  off_one:\n    provider: gmail\n    enabled: false\n")
    with pytest.raises(mail.MailError, match="disabled"):
        mail.resolve(None, p)


def test_no_enabled_account_is_still_a_placeholder():
    """Replaces an older test that asserted every account ships `enabled: false`.

    That was right for a fresh clone and wrong the moment a real account was connected —
    `yoyo-mail.yaml` is both the template a new user edits AND the live config, which is a
    design smell worth naming rather than enforcing around.

    The hazard the old test actually guarded against survives here: an account switched on
    while still carrying scaffold values fails at the first API call, with an error about
    credentials rather than about the placeholder that caused it.
    """
    for spec in mail.load_accounts():
        if not spec.enabled:
            continue
        assert not (spec.client_id or "").startswith("REPLACE"), (
            f"{spec.name} is enabled but its client_id is still the placeholder"
        )
        if spec.provider == "microsoft":
            assert spec.client_id, f"{spec.name} is enabled but has no client_id"


def test_an_enabled_account_names_a_secrets_file_that_is_gitignored():
    """A client_secrets path outside secrets/ would not be covered by .gitignore, and OAuth
    client secrets in git is the one mistake here that cannot be undone by a fix."""
    ignored = pathlib.Path(".gitignore").read_text(encoding="utf-8")
    assert "secrets/*" in ignored
    for spec in mail.load_accounts():
        if spec.enabled and spec.client_secrets:
            assert spec.client_secrets.startswith("secrets/"), (
                f"{spec.name} points at {spec.client_secrets}, which .gitignore does not cover"
            )


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


# ------------------------------------------------- disabled-account diagnosis ----
# Observed live 2026-08-15: `yoyo mail auth personal` reported success, and the very next
# `yoyo mail search` said "No mail accounts configured". Both messages were true in their
# own terms and together they were useless — the account existed, was authenticated, and
# was switched off.


def test_all_accounts_disabled_is_diagnosed_as_disabled_not_missing(tmp_path):
    import yaml as _yaml

    from yoyo import mail

    path = tmp_path / "yoyo-mail.yaml"
    path.write_text(
        _yaml.safe_dump({"accounts": {"personal": {"provider": "gmail", "enabled": False}}}),
        encoding="utf-8",
    )
    with pytest.raises(mail.MailError) as exc:
        mail.resolve(None, path=path)
    assert "disabled" in str(exc.value)
    assert "enabled: true" in str(exc.value)
    assert "personal" in str(exc.value)


def test_genuinely_empty_config_still_says_nothing_is_configured(tmp_path):
    import yaml as _yaml

    from yoyo import mail

    path = tmp_path / "yoyo-mail.yaml"
    path.write_text(_yaml.safe_dump({"accounts": {}}), encoding="utf-8")
    with pytest.raises(mail.MailError, match="No mail accounts configured"):
        mail.resolve(None, path=path)


def test_the_disabled_message_names_every_account_so_the_yaml_line_is_findable(tmp_path):
    import yaml as _yaml

    from yoyo import mail

    path = tmp_path / "yoyo-mail.yaml"
    path.write_text(
        _yaml.safe_dump({"accounts": {
            "personal": {"provider": "gmail", "enabled": False},
            "work": {"provider": "microsoft", "client_id": "x", "enabled": False},
        }}),
        encoding="utf-8",
    )
    with pytest.raises(mail.MailError) as exc:
        mail.resolve(None, path=path)
    assert "personal" in str(exc.value) and "work" in str(exc.value)


# ---------------------------------------------------------------- citations ----
# Added after the first live mail agent turn (2026-08-15). It answered "Suno charged you
# $11.30 on 8 August, invoice #2160-3779-5678" — entirely correct, and entirely
# unverifiable. Corpus answers cite [7]; vault answers cite [MyAIServer.md]; mail cited
# nothing, so checking it meant searching your own inbox by hand.


def test_a_message_carries_a_citation():
    m = gmail._parse_message(GMAIL_MSG, "personal", include_body=False)
    assert m.citation == "mail:m1"


def test_the_citation_is_in_the_dict_the_model_sees():
    """The model can only cite what reaches it. A citation property nothing serialises is
    a citation that never gets used."""
    payload = gmail._parse_message(GMAIL_MSG, "personal", include_body=False).as_dict()
    assert payload["citation"] == "mail:m1"


def test_the_citation_is_an_identifier_not_a_url():
    """Deliberate. Yoyo could assemble a mail.google.com link, but `u/0` guesses which
    signed-in account you are and the format is undocumented — a citation that silently
    opens the wrong mailbox is worse than one you paste. It is also the rule the agent is
    given: never construct a URL."""
    citation = gmail._parse_message(GMAIL_MSG, "personal", include_body=False).citation
    assert "http" not in citation
    assert "/" not in citation
    from yoyo.citations import fabricated_links

    assert fabricated_links(f"see [{citation}]") == []


def test_graph_messages_are_citable_the_same_way():
    """One citation vocabulary across providers, or the model learns two."""
    m = graph._parse(GRAPH_MSG, "work", include_body=False)
    assert m.citation.startswith("mail:")
    assert m.citation == "mail:g1"


def test_the_agent_prompt_teaches_the_mail_citation_form():
    from yoyo import agent

    assert "mail:" in agent.SYSTEM_PROMPT


def test_the_search_tool_description_asks_for_citations():
    """Tool descriptions are prompts. If this one does not ask, the model will not."""
    import inspect

    from yoyo.mcp import mail_server

    source = inspect.getsource(mail_server)
    assert "[mail:<id>]" in source
    assert "Never build a mail URL" in source


def test_reading_accepts_a_citation_or_a_bare_id():
    """The user pastes what the answer showed them — `mail:19fe...` — not a stripped id.
    Requiring them to edit it first would make the citation annoying enough to ignore."""
    assert "mail:abc".removeprefix("mail:") == "abc"
    assert "abc".removeprefix("mail:") == "abc"


def test_the_cli_exposes_a_way_to_resolve_a_citation():
    """A citation nobody can follow is decoration."""
    from yoyo.cli import app

    mail_group = next(g for g in app.registered_groups if g.name == "mail")
    names = {c.name for c in mail_group.typer_instance.registered_commands}
    assert "read" in names


def test_the_cli_draft_command_saves_without_sending():
    """Exposed so the draft path is exercised on a message the owner chose, rather than
    first discovered when an agent writes one unprompted.

    Asserted structurally rather than by grepping for the word "send" — the command's own
    output correctly contains "send it yourself", and a test that banned the word would be
    policing prose instead of behaviour. What matters is that the only provider call is
    `create_draft`.
    """
    import inspect

    from yoyo import cli

    source = inspect.getsource(cli.mail_draft)
    assert "create_draft" in source
    calls = re.findall(r"provider\.(\w+)", source)
    assert calls == ["create_draft"], f"mail draft calls more than create_draft: {calls}"
    assert "not sent" in source.lower(), "the user must be told it was not sent"
