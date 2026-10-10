"""`miles-area-import`: cut an area's tiles locally out of a regional
OpenStreetMap extract (a .osm.pbf the operator has on disk) and upload just
the result to a running miles-api, which finishes the import (elevation
prefetch + network build) without ever needing the extract itself.

A plain HTTP client, not a direct DB writer, mirroring route_import.py's
shape: the extract and the miles-api host are often different machines, and
cutting happens against a throwaway in-memory database, never the host's own.
"""

import sqlite3
import sys
from pathlib import Path

import click
import requests

from . import db, extracts, routes_spatial, tilecache, tiles


def _parse_latlng(value: str) -> tuple[float, float]:
    lat_s, lng_s = value.split(",")
    return float(lat_s), float(lng_s)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise click.BadParameter("--bbox must be south,west,north,east")
    return (parts[0], parts[1], parts[2], parts[3])


def _resolve_bbox_and_center(
    center: str | None, radius_km: float | None, bbox: str | None
) -> tuple[tuple[float, float, float, float], float, float, float]:
    """(bbox, center_lat, center_lng, radius_km) -- radius_km is exact for
    the --center/--radius-km form, and the bbox's own half-diagonal
    (an approximation used only for the area row's metadata, never for
    which tiles get cut) for the --bbox form."""
    if bbox is not None:
        if center is not None or radius_km is not None:
            raise click.UsageError("--bbox is exclusive of --center/--radius-km")
        parsed_bbox = _parse_bbox(bbox)
        south, west, north, east = parsed_bbox
        center_lat, center_lng = (south + north) / 2.0, (west + east) / 2.0
        approx_radius_km = routes_spatial.equirect_distance_m(center_lat, center_lng, north, east) / 1000.0
        return parsed_bbox, center_lat, center_lng, approx_radius_km
    if center is None or radius_km is None:
        raise click.UsageError("need --center and --radius-km, or --bbox")
    center_lat, center_lng = _parse_latlng(center)
    return tiles.bbox_for_center_radius(center_lat, center_lng, radius_km), center_lat, center_lng, radius_km


@click.command()
@click.option(
    "--pbf", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Regional .osm.pbf extract to cut the area from.",
)
@click.option("--center", default=None, help="lat,lng center -- pairs with --radius-km.")
@click.option("--radius-km", default=None, type=float, help="Radius in km -- pairs with --center.")
@click.option("--bbox", default=None, help="south,west,north,east -- alternative to --center/--radius-km.")
@click.option("--label", default=None, help="Area label on the miles-api host (default: the pbf's filename stem).")
@click.option("--pinned", is_flag=True, default=False, help="Keep the resulting area indefinitely instead of the normal unused-area GC.")
@click.option("--url", default="http://localhost:8000", help="Base URL of a running miles-api instance.")
def main(
    pbf: Path, center: str | None, radius_km: float | None, bbox: str | None,
    label: str | None, pinned: bool, url: str,
) -> None:
    area_bbox, center_lat, center_lng, resolved_radius_km = _resolve_bbox_and_center(center, radius_km, bbox)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    region = f"{extracts.LOCAL_REGION_PREFIX}{pbf.stem.replace(',', '_')}"
    record = extracts.register_local_extract(conn, pbf, region)
    tile_ids = extracts.cut_area(conn, record, area_bbox)

    tile_payloads = []
    for gx, gy in tile_ids:
        payload = tilecache.get_tile_payload(conn, gx, gy)
        tile_payloads.append({"gx": gx, "gy": gy, "elements": (payload or {}).get("elements", [])})

    resp = requests.post(
        f"{url}/api/areas/import-tiles",
        json={
            "label": label or pbf.stem,
            "center_lat": center_lat,
            "center_lng": center_lng,
            "radius_km": resolved_radius_km,
            "tiles": tile_payloads,
            "pinned": True if pinned else None,
            "source": f"extract:{region}",
        },
        timeout=120,
    )
    if not resp.ok:
        print(f"FAILED ({resp.status_code}) {resp.text[:300]}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()
    print(
        f"area {result['area_id']} \"{result['label']}\" -- {len(tile_payloads)} tiles uploaded, "
        f"status={result['status']} (network build running in the background)"
    )


if __name__ == "__main__":
    main()
