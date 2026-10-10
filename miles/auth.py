"""One-time Strava OAuth flow. Run once to populate .env with tokens."""
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
from stravalib import Client

from . import envfile, outbound, strava_client

_auth_code: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global _auth_code
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            _auth_code = params["code"][0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Auth complete. Return to terminal.")

    def log_message(self, *args: object) -> None:
        pass


@click.command()
def main() -> None:
    outbound.claim()
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        missing = [k for k, v in (("STRAVA_CLIENT_ID", client_id), ("STRAVA_CLIENT_SECRET", client_secret)) if not v]
        raise SystemExit(
            f"Missing {' and '.join(missing)} in .env — see README.md 'Get Strava API credentials' "
            "for where to find these on https://www.strava.com/settings/api."
        )

    try:
        client_id_int = int(client_id)
    except ValueError:
        raise SystemExit(
            f"STRAVA_CLIENT_ID in .env is not a number ({client_id!r}) — copy it again from "
            "https://www.strava.com/settings/api, see README.md 'Get Strava API credentials'."
        )

    client = Client()
    url = client.authorization_url(
        client_id=client_id_int,
        redirect_uri="http://localhost:8765/callback",
        scope=["read", "activity:read_all"],
    )
    print(f"Opening browser for Strava authorization...")
    webbrowser.open(url)
    print("Waiting for callback on http://localhost:8765/callback ...")

    server = HTTPServer(("localhost", 8765), _CallbackHandler)
    server.handle_request()

    if not _auth_code:
        raise SystemExit("No authorization code received.")

    token = strava_client.oauth_token_request({
        "client_id": str(client_id_int),
        "client_secret": client_secret,
        "code": _auth_code,
        "grant_type": "authorization_code",
    })
    env_file = strava_client.ENV_FILE or Path(".env")
    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", str(token["refresh_token"]))
    print(f"Refresh token saved to {env_file} — you're ready to run `miles-sync`.")


if __name__ == "__main__":
    main()
