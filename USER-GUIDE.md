# Yoyo — how to actually use it

This is the "you have forgotten everything, it is three months later" document. The README
is the status of the build; this is how to *drive* it, and why it behaves the way it does.

If you read one section, read **§2 (the three modes)** and **§7 (what it will get wrong)**.

---

## 1. Starting it

```powershell
cd C:\Projects\Yoyo\YoyoClient
.venv\Scripts\Activate.ps1
docker compose up -d          # Qdrant + SearXNG. Without this, retrieval runs at half strength
yoyo doctor                   # the gate — nothing below is trustworthy until this is green
yoyo serve                    # UI at http://127.0.0.1:8081
```

**`yoyo doctor` first, always.** It checks the tailnet hop, the API key, that every role
points at a capability the server actually serves, the tool-fidelity constraint, embeddings,
SQLite, Qdrant, your vault, and the optional configs. When something behaves oddly, this
turns "it's broken" into a named seam.

If Docker is down, retrieval silently degrades to keyword-only. It warns; it does not fail.
That is deliberate, and it is the kind of thing worth noticing in the log.

---

## 2. The three modes — the single most useful thing to know

Yoyo answers in three different ways, and picking wrong is the most common way to get a
disappointing answer. In the UI it is the dropdown; on the CLI it is the command.

| Mode | What it does | Use it when | Typical |
|---|---|---|---|
| **ask** | One retrieval, one model turn. **No tools at all.** | "What do my documents say about X" | ~8 s |
| **agent** | Tool-calling loop — mail, vault, corpus, web, tasks | Anything needing a live lookup | 10–35 s |
| **plan** | Splits the question, runs workers in parallel, synthesises | Multi-part or multi-source questions | 25–55 s |

**`ask` cannot search the web, read your mail, or look at your vault.** It only sees what
retrieval pulled from the corpus. If you ask it for today's news it will tell you it can't —
and if it slips, it will invent something. That is not a bug you can prompt around; it is
what "no tools" means. Asked for local news in `ask` mode once, it produced three plausible,
clickable, entirely invented news-site URLs.

**Use `plan` for questions with "and" in them.** Measured across five rounds (ADR-026): on a
three-part question the single agent was faster (32 s vs 51 s) and *wrong* — it attributed
corpus content to "your notes" when the vault was empty. The graph reported the vault was
empty and said how it checked. Slower and more honest.

Rule of thumb: **one thing → `agent`. Several things, or several sources → `plan`.**

---

## 3. Where your stuff lives — vault vs corpus

This distinction causes more confusion than anything else in the system, and the assistant
itself has got it wrong.

**The vault** is `C:\Projects\Yoyo\Notes`. Plain `.md` files, read live, always current.
Tools: `vault_search`, `vault_read`, `vault_list`, `vault_backlinks`.

**The corpus** is an embedded snapshot in SQLite + Qdrant. Semantic search, numbered chunk
citations like `[7]`. Tool: `search_corpus`.

They are **different stores and they drift.** A note you wrote this morning is in the vault
and not in the corpus until you ingest it. A document you ingested is in the corpus and may
not be a note at all.

```powershell
notepad C:\Projects\Yoyo\Notes\GB10.md      # add a note — that is the whole process
yoyo ingest C:\Projects\Yoyo\Notes          # make it semantically searchable
```

Ingest is content-hashed, so re-running over an unchanged folder is nearly free. Run it
after you write.

**The Vault map tab shows exactly this drift.** Blue = in the corpus. Grey = in the vault
only. Amber = empty. Dashed red = something links to it but you never wrote it. If a note
is grey and you expected the assistant to find it semantically, that is your answer.

Write `[[Another Note]]` anywhere and it becomes a link in the map. Obsidian is optional —
Yoyo just reads `.md` files — but it makes writing them pleasanter.

---

## 4. Citations — the habit worth keeping

Every answer cites what it used, in one of three vocabularies:

| Looks like | Is | Follow it with |
|---|---|---|
| `[7]` | corpus chunk | click it in the UI, or `yoyo search` |
| `[MyAIServer.md]` | vault note | click it, or open the file |
| `[mail:19fe2cb1d4f118a3]` | an email | click it, or `yoyo mail read mail:19fe...` |
| `https://…` | a web result | click it |

**In the UI every citation is clickable** and opens the original. That is the point of them.
An uncited claim about your own mail or notes is the one thing to be suspicious of — the
system is built so that facts come with a way to check them, and a bare assertion means
something skipped a step.

If you ever see **"removed N fabricated citation path(s)"**, that turn's model invented a
source and the scrubber caught it. The answer may still be right; the citation was not.

---

## 5. What each surface is for

| Surface | Reads | Writes | Notes |
|---|---|---|---|
| **Corpus** | ✅ | via `yoyo ingest` | Your ingested documents |
| **Vault** | ✅ | **drafts only** → `yoyo-drafts/` | Yoyo never edits your notes |
| **Mail** | ✅ | **drafts only**, never sends | Gmail, personal account |
| **Calendar** | read-only | ❌ never | Not yet authenticated |
| **Tasks** | ✅ | ❌ never ticks a box | Markdown checkboxes in the vault |
| **Web** | ✅ search + fetch | — | Through your own SearXNG |
| **Files** | read-only | ❌ | Scoped to `Notes`, allowlisted |

**The write asymmetry is not politeness, it is the design.** Yoyo proposes; you dispose.
Mail drafts land in Gmail unsent. Vault writes land in `yoyo-drafts/` and are excluded from
search, so the assistant cannot cite its own output back to you. Approving is you moving a
file or pressing send.

Since Yoyo now reads a real mailbox *and* fetches attacker-controlled web pages into the
same context, those limits are load-bearing security controls. Do not relax them for
convenience.

---

## 6. Everyday commands

```powershell
# Ask
yoyo ask "what do my documents say about the GB10?"
yoyo agent "what did Suno charge me and when?"
yoyo plan  "what's on my plate, and what did Alice email about it?"

# Look without a model — the fastest way to debug a bad answer
yoyo search "GB10 bandwidth"          # what retrieval actually finds
yoyo mail search "invoice"            # what Gmail actually returns
yoyo web search "GB10 specs"          # what the web actually says
yoyo tasks list                       # open checkboxes from your notes

# Follow a citation
yoyo mail read mail:19fe2cb1d4f118a3

# Housekeeping
yoyo ingest C:\Projects\Yoyo\Notes
yoyo stats
yoyo web egress                       # everything Yoyo has sent to the internet
yoyo backup F:\yoyo-backups
yoyo restore-drill --dest F:\yoyo-backups
```

**When an answer is wrong, run the no-model version first.** `yoyo search` tells you whether
retrieval found the material. If it did, the model ignored it. If it didn't, the corpus is
the problem. That one distinction saves most debugging.

---

## 7. What it will get wrong — read this one

Six times now, in different forms, the same failure: **confidently reporting something it
did not establish.** Each was found by using the system, not by reading the code.

1. Searched only the vault, then reported "not found" for something in the corpus.
2. A wildcard query matched zero results, and it concluded the thing did not exist.
3. Invented a `file:///Users/...` path for a real note.
4. Invented three news-site URLs when it had no web tool at all.
5. Answered "your notes say…" citing a **corpus** document, with an empty vault.
6. Answered half a multi-part question without flagging the missing half.

Guards now exist for all six — untried-source hints, an empty-result nudge, a path scrubber,
URL provenance checking, per-part accounting. **They reduce the rate; they do not make it
impossible.** So:

- **Uncited claims about your own data deserve suspicion.** Everything else has a citation.
- **Check the tool calls.** The UI lists them; the CLI prints them. If it answered a
  mail question without calling a mail tool, it made it up.
- **"Not found" is worth one retry** with a simpler word before you believe it.

None of this is unique to Yoyo. It is what running a language model over your own data
looks like when the failures are visible instead of hidden.

---

## 8. Voice

Everything local; no audio ever leaves the laptop.

```powershell
uv pip install -e ".[voice]"
yoyo voice status
yoyo say "the backup drill passed"
yoyo transcribe meeting.m4a --ingest    # transcript goes into the corpus, timestamped
yoyo talk                               # ENTER to start, ENTER to stop
```

The whisper model size is a real choice, not a default to accept: `small` mangles proper
nouns, which is most of what your corpus is made of ("Qdrant" → "quadrant"). Compare
`--model base|small|medium` on your own audio and judge on names, not speed.

---

## 9. Adding tools

**Someone else's MCP server** — config only, in `yoyo-mcp.yaml`:

```yaml
  files:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/Projects/Yoyo/Notes"]
    enabled: true
    prefix: fs
    allow: [read_text_file, list_directory, search_files]   # read-only, exclusive
```

**`allow` is exclusive and that matters.** Third-party servers choose their own surface and
it is often wider than you want — the filesystem server ships `write_file` and `move_file`
next to its read tools. An allowlist means the model never learns the write tools exist,
which is stronger than telling it not to call them. `deny` also exists and wins over `allow`.

Widen either list only with a reason you would be willing to write down.

**Your own server** — a new `src/yoyo/mcp/X_server.py`, a `yoyo mcp serve-x` command, and a
yaml block. The tool `description` is the prompt the model reads at the moment it decides to
act: put the constraints there, not in documentation.

After any change: `yoyo mcp list` shows what mounted and what failed. The UI header shows a
live tool count — if a tool you expect is missing, that is where you find out, rather than
from a confusing model error three questions later.

---

## 10. Things that are still true and worth remembering

- **BitLocker is off.** Your Gmail refresh token, corpus, and conversation history sit
  unencrypted. This is the only item on the list that gets worse the longer it waits.
- **Calendar is built but not authenticated.** Same Google project as mail; enable the
  Calendar API and run `yoyo calendar auth personal`.
- **Web queries leave your machine.** SearXNG means no vendor holds a profile tied to you,
  but the queries still go out. `yoyo web egress` is the log. Read it occasionally.
- **The corpus is tiny.** Chunking, fusion and the context budget have never met real
  volume. Expect surprises the first time you ingest something large.
- **`git commit` is manual**, by your choice.

---

## 11. If something breaks

| Symptom | First thing to check |
|---|---|
| Anything at all | `yoyo doctor` |
| "unknown tool 'x'" | `yoyo mcp list` — the server did not mount |
| Answers ignore recent notes | `yoyo ingest` — the corpus is a snapshot |
| Retrieval feels weak | Is Docker up? Qdrant down = keyword-only |
| Web search 403s | SearXNG's JSON API off — `search.formats: [html, json]` |
| Mail says "not configured" | `enabled: true` in `yoyo-mail.yaml` |
| Port already in use | `YOYO_API_PORT` in `.env` |
| Weird config behaviour | Duplicate keys in `.env` — doctor flags these |
| A wrong answer | `yoyo search` / `yoyo mail search` — did retrieval find it? |
