import click

from . import db, strava_client


@click.command()
@click.option("--full", is_flag=True, help="Ignore last sync date and fetch everything.")
def main(full: bool) -> None:
    conn = db.connect()
    db.init_db(conn)

    after = None if full else db.last_synced_date(conn)
    if after:
        print(f"Incremental sync: fetching activities after {after}")
    else:
        print("Full sync: fetching all activities (may take a few minutes)...")

    rows = []
    for i, row in enumerate(strava_client.get_activities(after_ts=after)):
        rows.append(row)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1} activities fetched...")

    if rows:
        db.upsert_activities(conn, rows)

    total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    print(f"Done. {len(rows)} new/updated. {total} total in DB.")


if __name__ == "__main__":
    main()
