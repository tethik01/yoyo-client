"""MCP integration.

Two directions, both routed through `yoyo.tools.Registry`:

- **client** — mounts external MCP servers as Yoyo tools (`client.mount_all`)
- **servers** — exposes Yoyo's own capabilities over MCP:
    - `vault_server`  the Obsidian vault (read + drafts-only write)
    - `corpus_server` the ingested corpus (search, read, stats)

The vault server is mounted by the client adapter in normal use, which means the same code
path serves Claude Desktop and Yoyo alike.
"""
