"""Inspect and clear the 45-day entity cooldown.

The cooldown layer blocks a topic whose named entities were covered recently.
That is the point of it — but a bad research call can write entities that block
unrelated topics, and there was otherwise no way to undo that without editing
the database by hand.

    python scripts/reset_cooldown.py                  # show what is blocked
    python scripts/reset_cooldown.py --clear          # forget all of them
    python scripts/reset_cooldown.py --clear Roopkund # forget one entity
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import Settings  # noqa: E402
from engine.store import Store  # noqa: E402


def listing(store: Store, days: int) -> int:
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT e.entity, MAX(e.seen_at) AS last, "
            "       COUNT(*) AS n, p.slug "
            "FROM entities e LEFT JOIN plans p ON p.plan_id = e.plan_id "
            "GROUP BY e.entity ORDER BY last DESC").fetchall()

    if not rows:
        print("No entities recorded. Nothing is on cooldown.")
        return 0

    now = datetime.now(timezone.utc)
    print(f"{'entity':<22} {'last used':<12} {'free in':<9} recorded against")
    print("-" * 74)
    for row in rows:
        seen = datetime.fromisoformat(row["last"])
        age = (now - seen).days
        remaining = days - age
        state = f"{remaining}d" if remaining > 0 else "free"
        print(f"{row['entity']:<22} {seen.date()!s:<12} {state:<9} "
              f"{row['slug'] or '(plan gone)'}")
    print(f"\n{len(rows)} entities. Cooldown window is {days} days.")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear", nargs="?", const="__all__", default=None,
                        metavar="ENTITY",
                        help="forget every entity, or just the one named")
    parser.add_argument("--db", default=None, help="override the database")
    args = parser.parse_args()

    settings = Settings()
    store = Store(args.db or settings.db_path)
    store.init()

    if args.clear is None:
        listing(store, settings.entity_cooldown_days)
        print("\nRe-run with --clear to forget them, or --clear <Entity> for "
              "a single one.")
        return 0

    if args.clear == "__all__":
        removed = store.clear_entities()
        print(f"Cleared {removed} entity rows. Every topic is open again.")
        print("Note: this is the policy defence against covering the same "
              "subject repeatedly. Clear it when the data is wrong, not to "
              "get around it.")
        return 0

    target = args.clear.strip().lower()
    with sqlite3.connect(store.db_path) as conn:
        removed = conn.execute("DELETE FROM entities WHERE entity=?",
                               (target,)).rowcount
    if removed:
        print(f"Cleared {removed} row(s) for '{args.clear}'.")
    else:
        print(f"No rows for '{args.clear}'. Run without --clear to see what "
              f"is recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
