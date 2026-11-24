# Sunday Drive pipeline, part 3: put the staged network live, or roll back.
#
# Run in the same Colab session after part 2 printed "ALL CHECKS PASSED".
# Each swap is a single transaction: all of it happens or none of it does.
# After any swap, hit the live API once (/fastest and /wander) to confirm.

from sqlalchemy import text


def swap(engine, problems, schema="stage"):
    """Move live tables to `archive` and the staged tables to `public`."""
    assert not problems, f"Part 2 checks failed: {problems}"
    if input("Type SWAP to put the staged network live: ").strip() != "SWAP":
        raise SystemExit("cancelled, nothing changed")
    steps = [
        "create schema if not exists archive",
        "drop table if exists archive.ways cascade",      # the previous archive
        "drop table if exists archive.nodes cascade",
        "alter table public.ways  set schema archive",    # current live -> archive
        "alter table public.nodes set schema archive",
        f"alter table {schema}.ways  set schema public",  # staged -> live
        f"alter table {schema}.nodes set schema public",
    ]
    with engine.begin() as c:
        c.execute(text("set local lock_timeout = '15s'"))
        for s in steps:
            c.execute(text(s))
    print("Swapped. The staged network is now live. Archive holds the previous one.")


def rollback(engine, schema="stage"):
    """Put the archived network back live; the bad one goes back to `schema`."""
    steps = [
        f"create schema if not exists {schema}",
        f"alter table public.ways  set schema {schema}",   # bad network -> stage
        f"alter table public.nodes set schema {schema}",
        "alter table archive.ways  set schema public",     # previous -> live
        "alter table archive.nodes set schema public",
    ]
    with engine.begin() as c:
        c.execute(text("set local lock_timeout = '15s'"))
        for s in steps:
            c.execute(text(s))
    print("Rolled back. The previous network is live again.")


# Usage (in Colab, after part 2):
#   swap(engine, problems)
#   rollback(engine)
