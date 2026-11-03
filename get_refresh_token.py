#!/usr/bin/env python3
"""One-time local helper script to generate a SPOTIFY_REFRESH_TOKEN."""

import base64
import http.server
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"


def exchange_code_for_token(client_id: str, client_secret: str, code: str) -> dict:
    """Exchange an OAuth authorization code for access and refresh tokens."""
    creds = f"{client_id}:{client_secret}".encode("utf-8")
    basic_auth = base64.b64encode(creds).decode("ascii")

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    print("=== Spotify Playlist Guardian: Refresh Token Generator ===\n")
    print("Make sure your Spotify Developer Dashboard app has this exact Redirect URI:")
    print(f"  {REDIRECT_URI}\n")

    client_id = input("Enter SPOTIFY_CLIENT_ID: ").strip()
    client_secret = input("Enter SPOTIFY_CLIENT_SECRET: ").strip()

    if not client_id or not client_secret:
        print("Error: Client ID and Client Secret cannot be empty.")
        sys.exit(1)

    auth_params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "show_dialog": "true",
        }
    )
    auth_url = f"https://accounts.spotify.com/authorize?{auth_params}"

    captured: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            qs = urllib.parse.parse_qs(parsed.query)
            if "error" in qs:
                captured["error"] = qs["error"][0]
                msg = f"Authorization failed: {captured['error']}"
            elif "code" in qs:
                captured["code"] = qs["code"][0]
                msg = "Authorization complete! Return to your terminal for the Refresh Token. You may close this tab."
            else:
                captured["error"] = "missing_code"
                msg = "Missing 'code' parameter in callback URL."

            html_page = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Spotify Playlist Guardian</title></head>"
                f"<body style='font-family:sans-serif;padding:40px'><h2>{msg}</h2></body></html>"
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_page)))
            self.end_headers()
            self.wfile.write(html_page)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = http.server.HTTPServer(("127.0.0.1", 8888), CallbackHandler)

    print("\nOpening browser for Spotify authorization...")
    print("If the browser does not open automatically, visit this URL manually:\n")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for authorization callback on http://127.0.0.1:8888/callback ...")
    while "code" not in captured and "error" not in captured:
        server.handle_request()
    server.server_close()

    if "error" in captured:
        print(f"\nAuthorization canceled or failed: {captured['error']}")
        sys.exit(1)

    try:
        tokens = exchange_code_for_token(client_id, client_secret, captured["code"])
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print(f"\nToken exchange failed (HTTP {exc.code}): {err_body}")
        sys.exit(1)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("\nFailed to obtain refresh_token. API response:", tokens)
        sys.exit(1)

    print("\n" + "=" * 65)
    print("SUCCESS! Save this value in GitHub Secrets as SPOTIFY_REFRESH_TOKEN:")
    print("=" * 65)
    print(f"\n{refresh_token}\n")
    print("=" * 65)


if __name__ == "__main__":
    main()
