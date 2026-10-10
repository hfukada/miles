"""`miles-area-refresh`: re-fetch an area's map data or a place's geocode on
request, on the host that owns the database."""

import click

from . import areas, db, events, geocode, outbound


@click.command()
@click.argument("label", required=False)
@click.option("--geocode", "geocode_place", default=None, help="Look this place up in Nominatim again and update its cached coordinates.")
@click.option(
    "--force-extract", is_flag=True, default=False,
    help="Download the area's regional extract again unconditionally, replacing a file that is bad but still registered.",
)
def main(label: str | None, geocode_place: str | None, force_extract: bool) -> None:
    if label is None and geocode_place is None:
        raise click.UsageError("give an area LABEL to refresh, or --geocode PLACE")
    outbound.claim()
    events.install()
    conn = db.connect()
    if geocode_place is not None:
        try:
            found = geocode.lookup(conn, geocode_place, refresh=True)
        except geocode.GeocodeError as e:
            raise click.ClickException(str(e)) from e
        click.echo(f"{geocode_place}: {found['lat']:.5f}, {found['lng']:.5f} ({found['display_name']})")
        if "note" in found:
            click.echo(f"note: {found['note']}")
    if label is not None:
        try:
            status = areas.refresh_area(conn, label, force_extract=force_extract)
        except areas.AreaNotFoundError as e:
            raise click.ClickException(str(e)) from e
        if status["status"] == "error":
            raise click.ClickException(status.get("error") or "refresh failed")
        click.echo(f"{label}: {status['status']}, data as of {status.get('data_as_of')}")


if __name__ == "__main__":
    main()
