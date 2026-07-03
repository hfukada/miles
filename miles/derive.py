"""
Full recompute of every value derived from raw synced rows (activities/laps).

Raw synced data is ground truth; everything here is rebuildable from it. `derive_all`
always recomputes from scratch — no incremental invalidation, ever — so a `git pull`
that changes a classifier or threshold only needs a rerun to take effect everywhere,
including ad-hoc `run_sql` queries.
"""

import sqlite3
from datetime import datetime, timezone

from .classifier import classify_laps
from .inference import apply_inference

# Bump whenever any classifier or threshold feeding a derived value changes, so
# ensure_derived() knows stale rows need a full recompute.
DERIVE_VERSION = "1"

# Laps below this floor are never classified (too short/slow to read a speed signal).
LAP_MIN_DISTANCE_M = 200
LAP_MIN_MOVING_TIME_S = 45


def _type_laps(conn: sqlite3.Connection) -> int:
    """Recompute laps.lap_type for every activity that has laps. Returns count typed."""
    conn.execute("UPDATE laps SET lap_type = NULL")

    activity_ids = [
        int(row["activity_id"])
        for row in conn.execute("SELECT DISTINCT activity_id FROM laps").fetchall()
    ]

    typed = 0
    for activity_id in activity_ids:
        laps = conn.execute("""
            SELECT lap_id, distance_m, moving_time_s, average_speed_mps, average_heartrate
            FROM laps WHERE activity_id = ? ORDER BY lap_index
        """, [activity_id]).fetchall()

        classifiable = [
            i for i, lap in enumerate(laps)
            if lap["distance_m"] is not None and float(lap["distance_m"]) >= LAP_MIN_DISTANCE_M
            and lap["moving_time_s"] is not None and int(lap["moving_time_s"]) >= LAP_MIN_MOVING_TIME_S
            and lap["average_speed_mps"] is not None and float(lap["average_speed_mps"]) > 0
        ]
        if not classifiable:
            continue

        sub_types = classify_laps(
            speeds=[float(laps[i]["average_speed_mps"]) for i in classifiable],
            distances_m=[float(laps[i]["distance_m"]) for i in classifiable],
            heartrates=[
                float(laps[i]["average_heartrate"]) if laps[i]["average_heartrate"] is not None else None
                for i in classifiable
            ],
        )
        conn.executemany(
            "UPDATE laps SET lap_type = ? WHERE lap_id = ?",
            [(t, int(laps[i]["lap_id"])) for i, t in zip(classifiable, sub_types)],
        )
        typed += len(classifiable)

    return typed


def derive_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Full recompute of every derived value from raw synced data. Never incremental."""
    counts: dict[str, int] = {}

    inferred = apply_inference(conn)
    for run_type, n in inferred.items():
        counts[f"inferred_{run_type}"] = n

    counts["laps_typed"] = _type_laps(conn)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('derive_version', ?)", (DERIVE_VERSION,)
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('derived_at', ?)", (now,)
    )
    conn.commit()

    return counts


def ensure_derived(conn: sqlite3.Connection) -> None:
    """Run derive_all if the DB's derived values are missing or from a stale version."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'derive_version'").fetchone()
    if row is None or row["value"] != DERIVE_VERSION:
        derive_all(conn)


def main() -> None:
    from . import db

    conn = db.connect()
    db.init_db(conn)
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")


if __name__ == "__main__":
    main()
