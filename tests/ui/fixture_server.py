"""A throwaway Yoyo for the browser smoke test — real code, disposable data.

Everything lands in /tmp/uitest/work: its own SQLite file, its own token, and a three-note
vault with one note the owner wrote, one linked-but-unwritten, and one page written by Yoyo
(so the map's colouring and the search exclusion are both exercised). Three claims are queued
for review, because an empty queue would test the empty state and nothing else.

It deliberately does NOT reach MyAIServer. Doctor will report several failures and that is
the point: the UI's job is to display a failing check clearly, and a test against a healthy
system never sees that path.
"""

import os, sys, pathlib, threading
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
root = pathlib.Path("/tmp/uitest/work"); root.mkdir(parents=True, exist_ok=True)
os.environ["YOYO_API_PORT"] = "8099"
os.environ["YOYO_VAULT_PATH"] = str(root / "vault")

# a small vault: one real note, one linked-but-unwritten, one Yoyo memory page
v = root / "vault"; (v / "yoyo-memory" / "people").mkdir(parents=True, exist_ok=True)
(v / "Trip.md").write_text("# Trip\n\nLisbon in March with [[Priya]] and [[Alice]].\n", encoding="utf-8")
(v / "Alice.md").write_text("# Alice\n\nWorks with [[Trip]].\n", encoding="utf-8")
(v / "yoyo-memory" / "people" / "Priya.md").write_text(
    "---\nabout: Priya\nkind: person\n---\n\n# Priya\n\n- flying to Lisbon\n", encoding="utf-8")

from yoyo.storage import db as db_mod
dbpath = root / "yoyo.db"
db_mod.DEFAULT_PATH = dbpath
_real = db_mod.connection
db_mod.connection = lambda p=None: _real(p or dbpath)

from yoyo import auth
auth.token_path = lambda: root / "ui-token"

from yoyo.memory import review
from yoyo.memory.wiki import Claim
import yoyo.api as api

db_mod.migrate(dbpath)
review.propose([
    Claim(subject="Priya", kind="person", claim="Priya is Bhavin's sister",
          quote="my sister Priya", source="conversation://1"),
    Claim(subject="Priya", kind="person", claim="Priya is flying to Lisbon on the 14th",
          quote="flying to Lisbon on the 14th", source="conversation://1"),
    Claim(subject="Lisbon", kind="place", claim="Lisbon is where the March trip goes",
          quote="Lisbon in March", source="vault://Trip.md"),
])

import uvicorn
uvicorn.run(api.app, host="127.0.0.1", port=8099, log_level="warning")
