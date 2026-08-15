# ADR-029: Web search through self-hosted SearXNG; egress logged, not blocked

> Mirrored from the Claude project decision log on 2026-08-15. The project doc is
> authoritative; if they disagree, this mirror is the bug.

- **Status:** ACCEPTED (2026-08-15) · **Partially addresses:** OQ5, open since ADR-021
- **Overlays:** ADR-009 (the Squid egress boundary, void on Windows)

## Context

Yoyo's premise is that nothing leaves the network. Web search cannot honour that — a search
is by definition a disclosure. The honest options were "don't build it" or "build it and say
exactly what it costs". The second was chosen.

## SearXNG, not a search API

**Buys:** no single vendor holds an API key tied to the owner and accumulates an
attributable log of every question the assistant was ever asked.

**Does not buy:** privacy. The queries still leave; SearXNG proxies them. Anyone claiming a
self-hosted metasearch makes search local-first is wrong, and `yoyo-search.yaml` says so in
its header rather than letting the reader infer purity from "self-hosted".

## The egress log: visibility restored, control not

Every search and fetch appends to `data/egress.jsonl` (`yoyo web egress`).

This is **not** ADR-009's replacement. It blocks nothing, it is written by the process it
audits, and a compromised Yoyo would simply not call it. It is still worth having: OQ5's
practical question is "what has this thing been sending?", and a log answers that where
nothing did. **OQ5 moves ❌ → 🟡, not ✅**, and only for web traffic — the model endpoint,
OAuth refreshes and fastembed downloads stay unaudited.

The logger never raises. An audit that can break what it audits gets switched off.

## Fetched pages are untrusted, framed not sanitised

`web_fetch` is the first place an outsider can write into Yoyo's context. Content is wrapped
in an explicit untrusted marker **with the content**, not only in a tool description read
thousands of tokens earlier.

Injection attempts are deliberately **not stripped** — filtering means enumerating the
phrasings, which is unwinnable, and a half-stripped attack reads as legitimate. Framing plus
visibility is weaker but honest: the model is told where the boundary is, and the attempt
survives so it can be reported.

A mitigation, not a solution. A good injection against a model that also has mailbox read
access is a real risk, and it is now real in this system. Compensating controls: Yoyo cannot
send mail, cannot write outside `yoyo-drafts/`, cannot write to the calendar at all, and
cannot reach private addresses.

## The SSRF gate is code, not config

Private, loopback, link-local, reserved and multicast addresses are refused **after DNS
resolution** — `evil.com` resolving to `127.0.0.1` is the standard bypass and a string check
waves it through. Redirects are re-checked at their destination. Only `http`/`https`;
`file:///C:/Users/...` through a web fetcher reads the disk.

Not adjustable, no tool argument overrides it. Any such argument is the one an injected page
would ask for.

## Consequences

- The "nothing leaves the network" claim is now qualified in the README, not buried.
- **Prompt injection is in the threat model**, and Yoyo has read access to a real mailbox.
  The write asymmetries that looked like ergonomics are now load-bearing security controls.
- `yoyo web egress` should be read occasionally — it is the only way to notice an agent
  putting private context into a public query, which the prompt forbids but nothing enforces.

## Open

- **Nothing has run against the live SearXNG.** All 42 tests are offline. Its JSON API must
  be enabled (`search.formats: [html, json]`) or every query 403s with an unhelpful page.
- Whether an agent respects the no-private-data rule is untested, and only observable
  through the egress log.
- The research graph (search → fetch → synthesise → ingest) is not built.
- **Blocking** remains unaddressed. A Windows-side proxy is the only thing that closes OQ5.

## Rejected

- A hosted search API — better results, at the cost of an attributable vendor-side log.
- Stripping injection attempts — unwinnable, and half-stripped looks legitimate.
- A `force` argument on the SSRF gate.
- Claiming this closes OQ5. It logs; it does not block.
