"""Agent API keys: minted per customer agent, stored hashed, revocable live.

A customer creates a key for each agent in their workspace settings. The key
carries the workspace (tenant) and an access level. The server only stores a
SHA-256 hash of the secret part, the same way you would store a password.

The store is a JSON file. ``lookup()`` re-reads it whenever it changes on
disk, so a key revoked from another process (the CLI below, or your admin
UI writing the same store) is refused on the very next request, with no
restart. In production, point ``lookup()`` at your database instead.

    python -m mcp_server.keys create --workspace acme --name acme-ops-agent --access editor
    python -m mcp_server.keys list
    python -m mcp_server.keys revoke <key_id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Access levels a customer picks when minting a key, and the roles each one
# grants. Tools declare the role they need (see server.py).
ACCESS_LEVELS: dict[str, list[str]] = {
    "reader": ["reader"],
    "editor": ["reader", "editor"],
    "admin": ["reader", "editor", "admin"],
    # People who review destructive calls. Not meant for agents.
    "approver": ["reader", "approver"],
    # A workspace owner's personal key: everything, but still never their own approvals.
    "owner": ["reader", "editor", "admin", "approver"],
}

PREFIX = "tbk_"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class KeyStore:
    """API keys in a JSON file, hashed, reloaded when the file changes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._version: tuple[int, int, int] | None = None
        self._records: dict[str, dict[str, Any]] = {}

    # -- used by the server on every request -----------------------------------

    def lookup(self, presented: str) -> dict[str, Any] | None:
        """APIKeyAuth(lookup=...) hook: the key's identity, or None to refuse it."""
        key_id, _, secret = presented.removeprefix(PREFIX).partition(".")
        if not presented.startswith(PREFIX) or not secret:
            return None
        record = self._load().get(key_id)
        if record is None or record.get("revoked_at"):
            return None
        if not secrets.compare_digest(_hash(secret), record["secret_sha256"]):
            return None
        return {
            "client_id": record["client_id"],
            "tenant_id": record["workspace"],
            "roles": record["roles"],
        }

    # -- used by the CLI, the demo and the tests ----------------------------------

    def create(self, *, workspace: str, name: str, access: str) -> tuple[str, dict[str, Any]]:
        """Mint a key. The plain key is returned once and never stored."""
        if access not in ACCESS_LEVELS:
            raise ValueError(f"access must be one of {sorted(ACCESS_LEVELS)}")
        key_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(24)
        record = {
            "key_id": key_id,
            "client_id": name,
            "workspace": workspace,
            "access": access,
            "roles": ACCESS_LEVELS[access],
            "secret_sha256": _hash(secret),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "revoked_at": None,
        }
        with self._lock:
            records = dict(self._load())
            records[key_id] = record
            self._save(records)
        return f"{PREFIX}{key_id}.{secret}", record

    def revoke(self, key_id: str) -> bool:
        """Revoke a key. Returns False when there is no such active key."""
        with self._lock:
            records = dict(self._load())
            record = records.get(key_id)
            if record is None or record["revoked_at"]:
                return False
            records[key_id] = {**record, "revoked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._save(records)
        return True

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "secret_sha256"} for r in self._load().values()]

    # -- file handling ----------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._records, self._version = {}, None
            return self._records
        # _save() replaces the file, so the inode changes on every write,
        # whichever process made it.
        version = (st.st_ino, st.st_mtime_ns, st.st_size)
        if version != self._version:
            self._records = json.loads(self.path.read_text() or "{}")
            self._version = version
        return self._records

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        # Write a temp file and rename it, so a reader never sees half a file.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".keys-")
        with os.fdopen(fd, "w") as fh:
            json.dump(records, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        st = self.path.stat()
        self._records, self._version = records, (st.st_ino, st.st_mtime_ns, st.st_size)


def main(argv: list[str] | None = None) -> int:
    from .config import Settings

    parser = argparse.ArgumentParser(prog="python -m mcp_server.keys", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create", help="mint a key for an agent (printed once)")
    create.add_argument("--workspace", required=True)
    create.add_argument("--name", required=True, help="the agent's client id, e.g. acme-ops-agent")
    create.add_argument("--access", choices=sorted(ACCESS_LEVELS), default="reader")
    sub.add_parser("list", help="list keys (never the secrets)")
    revoke = sub.add_parser("revoke", help="revoke a key; the running server refuses it at once")
    revoke.add_argument("key_id")
    args = parser.parse_args(argv)

    store = KeyStore(Settings.from_env().keys_file)
    if args.cmd == "create":
        key, record = store.create(workspace=args.workspace, name=args.name, access=args.access)
        print(f"key_id {record['key_id']}  {record['client_id']}  {record['workspace']}  {record['access']}")
        print(f"key    {key}")
        print("Store it now: it is not shown again.", file=sys.stderr)
    elif args.cmd == "list":
        for r in store.list():
            state = f"revoked {r['revoked_at']}" if r["revoked_at"] else "active"
            print(f"{r['key_id']}  {r['client_id']:<22} {r['workspace']:<8} {r['access']:<9} {state}")
    else:
        if not store.revoke(args.key_id):
            print(f"no active key {args.key_id}", file=sys.stderr)
            return 1
        print(f"revoked {args.key_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
