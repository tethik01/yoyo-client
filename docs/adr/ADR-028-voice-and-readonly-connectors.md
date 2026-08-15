# ADR-028: Voice runs locally; calendar and tasks are read-only


- **Status:** ACCEPTED (2026-08-15)
- **Supersedes:** ADR-017-GB10 (voice as CPU-pinned containers on the box), which ADR-021
  left "deferred, not decided"

## Voice is local, and that is a security boundary

STT is faster-whisper (CTranslate2); TTS is Piper with Windows SAPI as a zero-install
fallback. Both run on the laptop. **No audio crosses the tailnet.**

- Audio is the most sensitive input Yoyo will handle. A meeting recording captures people
  who never agreed to a transcription pipeline; their consent was to the meeting.
- ADR-009's egress audit does not exist on Windows (OQ5), so outbound traffic is unaudited.
- Routing audio to MyAIServer would add an unaudited flow of the most sensitive data type
  available, to buy latency on a task already faster than realtime on CPU.

Only transcribed **text** reaches a model. A structural test asserts no module under
`voice/` imports `httpx`, `requests`, `urllib.request` or `aiohttp`.

**SAPI exists so the feature is not dead on arrival** — Piper sounds far better but needs a
`.onnx` download. Two engine hazards are handled in code: VAD filtering is on by default
(Whisper hallucinates fluent sentences out of silence — the same failure class as the
fabricated citation in ADR-026), and the config states that `small` is a starting point to
measure, because model size matters most on proper nouns, which is what this corpus is made
of.

## Tasks are read-only, over the vault, with no new store

Obsidian checkboxes parsed into structured items. No database, no sync, no credentials.

**Yoyo does not tick boxes** — this follows from the vault's existing write asymmetry, not a
new rule. Marking a task done is the silent state change that asymmetry prevents. Drafts are
excluded from collection, so a task Yoyo invented cannot surface in "what am I late on" as a
fabrication with a deadline attached.

Four due-date dialects parsed; relative words deliberately not interpreted. A wrong due date
silently reorders what the user believes is urgent.

## Calendar is read-only, permanently, and shares mail's OAuth app

Scopes `calendar.readonly` and `Calendars.Read`. No write path at token level.

**Why no draft path like mail?** A calendar has no inert state. An email draft sits doing
nothing until sent; a "tentative" event is already on other people's calendars and has
already sent invitations. There is no way to *propose* a meeting without acting. If wanted
later, the honest shape is a draft **note** in the vault.

**Shared registration** — same Google Desktop-app client, same Entra Application ID — so
calendar is one API enable plus one scope rather than a second setup session. Tokens are
stored separately so revoking one service does not revoke the other.

**Timezones get the most test coverage here.** A bad citation is visibly wrong; a meeting an
hour off looks normal until it is missed. Two traps handled: Google needs
`singleEvents=true` and Graph needs `/calendarView`, or a recurring stand-up is invisible
on any given day; and Graph's 7 fractional-second digits broke a naive trim that dropped the
trailing offset, silently converting every Microsoft event to UTC.
