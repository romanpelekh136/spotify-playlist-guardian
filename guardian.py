#!/usr/bin/env python3
"""
Spotify Playlist Guardian — serverless playlist monitor & backup tool for GitHub Actions.
Uses Python 3 standard library only (zero external dependencies).
"""

import base64
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT_DIR = pathlib.Path(__file__).resolve().parent
SNAPSHOT_PATH = ROOT_DIR / "data" / "snapshot.json"
PLAYLIST_MD_PATH = ROOT_DIR / "PLAYLIST.md"
REMOVED_MD_PATH = ROOT_DIR / "REMOVED_HISTORY.md"


def load_dotenv(dotenv_path: pathlib.Path) -> None:
    """Load environment variables from a local .env file if present."""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def extract_playlist_ids(raw_value: str) -> list[str]:
    """Extract Spotify playlist IDs from full URLs, URIs, or comma-separated raw IDs."""
    ids: list[str] = []
    for part in raw_value.split(","):
        token = part.strip()
        if not token:
            continue
        match_url = re.search(r"playlist/([A-Za-z0-9]+)", token)
        match_uri = re.search(r"spotify:playlist:([A-Za-z0-9]+)", token)
        if match_url:
            ids.append(match_url.group(1))
        elif match_uri:
            ids.append(match_uri.group(1))
        else:
            clean_id = token.split("?")[0].strip("/")
            ids.append(clean_id)
    return ids


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    retries: int = 2,
) -> dict:
    """Perform an HTTP request and parse its JSON response with basic retry logic."""
    req_headers = headers or {}
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                retry_after = int(exc.headers.get("Retry-After", "3"))
                time.sleep(min(retry_after, 15))
                continue
            if exc.code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return {}


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a long-lived refresh token for a short-lived Spotify access token."""
    creds = f"{client_id}:{client_secret}".encode("utf-8")
    basic_auth = base64.b64encode(creds).decode("ascii")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")

    try:
        payload = http_json(
            "https://accounts.spotify.com/api/token",
            method="POST",
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=body,
        )
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Failed to refresh Spotify access token (HTTP {exc.code}): {err_text}"
        ) from exc

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Spotify response did not include access_token: {payload}")
    return token


def fetch_playlist_name(access_token: str, playlist_id: str) -> str:
    """Fetch the human-readable title of a playlist."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name"
    try:
        data = http_json(url, headers=headers)
        return data.get("name") or playlist_id
    except urllib.error.HTTPError:
        return playlist_id


def parse_track_entry(entry: dict) -> dict | None:
    """Parse a playlist item supporting both Feb 2026 ('item') and legacy ('track') schemas."""
    if not isinstance(entry, dict):
        return None

    raw_track = (
        entry.get("item")
        if isinstance(entry.get("item"), dict)
        else (entry.get("track") if isinstance(entry.get("track"), dict) else None)
    )
    if not raw_track:
        return None

    track_id = raw_track.get("id")
    uri = raw_track.get("uri") or ""
    name = (raw_track.get("name") or "").strip()
    if not name and not track_id and not uri:
        return None

    artists_raw = raw_track.get("artists") or []
    artists = ", ".join(
        a.get("name", "").strip()
        for a in artists_raw
        if isinstance(a, dict) and a.get("name")
    ) or "Unknown Artist"

    album_obj = raw_track.get("album")
    album = album_obj.get("name", "").strip() if isinstance(album_obj, dict) else ""

    key = track_id or uri or f"local:{artists}:{name}"
    spotify_url = (
        raw_track.get("external_urls", {}).get("spotify")
        if isinstance(raw_track.get("external_urls"), dict)
        else None
    )
    if not spotify_url and track_id:
        spotify_url = f"https://open.spotify.com/track/{track_id}"

    return {
        "key": key,
        "id": track_id,
        "uri": uri,
        "name": name or "Untitled",
        "artists": artists,
        "album": album,
        "added_at": entry.get("added_at") or "",
        "url": spotify_url or "",
    }


def fetch_playlist_tracks(access_token: str, playlist_id: str) -> list[dict]:
    """Fetch all tracks in a playlist with pagination (limit=50)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    next_url: str | None = (
        f"https://api.spotify.com/v1/playlists/{playlist_id}/items?limit=50&offset=0"
    )

    tracks_by_key: dict[str, dict] = {}
    used_fallback = False

    while next_url:
        try:
            page = http_json(next_url, headers=headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not used_fallback and "/items" in next_url:
                used_fallback = True
                next_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=50&offset=0"
                continue
            err_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Failed to fetch playlist {playlist_id} (HTTP {exc.code}): {err_text}"
            ) from exc

        for entry in page.get("items") or []:
            parsed = parse_track_entry(entry)
            if parsed and parsed["key"] not in tracks_by_key:
                tracks_by_key[parsed["key"]] = parsed

        next_url = page.get("next")

    return list(tracks_by_key.values())


def load_previous_snapshot() -> dict:
    if not SNAPSHOT_PATH.is_file():
        return {"playlists": {}}
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"playlists": {}}


def save_snapshot(snapshot: dict) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def write_playlist_markdown(snapshot: dict, updated_str: str) -> None:
    lines = [
        "# Playlist Snapshot",
        "",
        f"> Last synced: `{updated_str}`",
        "",
    ]
    for pid, pdata in snapshot.get("playlists", {}).items():
        pname = pdata.get("name") or pid
        tracks = pdata.get("tracks") or []
        lines.append(f"## {pname} ({len(tracks)} tracks)")
        lines.append("")
        lines.append("| # | Artist | Track | Album | Link |")
        lines.append("|---:|---|---|---|---|")
        for idx, t in enumerate(tracks, start=1):
            link = f"[Spotify]({t['url']})" if t.get("url") else "—"
            lines.append(
                f"| {idx} | {escape_md_cell(t['artists'])} | "
                f"{escape_md_cell(t['name'])} | {escape_md_cell(t.get('album', ''))} | {link} |"
            )
        lines.append("")

    PLAYLIST_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def append_removed_history(removed_events: list[tuple[str, dict]], date_str: str) -> None:
    if not removed_events:
        return

    if not REMOVED_MD_PATH.is_file():
        header = (
            "# Removed Tracks History\n\n"
            "Log of tracks detected as removed from monitored playlists.\n\n"
            "| Detected At | Playlist | Artist | Track | Link |\n"
            "|---|---|---|---|---|\n"
        )
        REMOVED_MD_PATH.write_text(header, encoding="utf-8")

    new_rows: list[str] = []
    for playlist_name, track in removed_events:
        link = f"[Open in Spotify]({track['url']})" if track.get("url") else "—"
        new_rows.append(
            f"| {date_str} | {escape_md_cell(playlist_name)} | "
            f"{escape_md_cell(track['artists'])} | {escape_md_cell(track['name'])} | {link} |"
        )

    existing = REMOVED_MD_PATH.read_text(encoding="utf-8")
    if not existing.endswith("\n"):
        existing += "\n"
    REMOVED_MD_PATH.write_text(existing + "\n".join(new_rows) + "\n", encoding="utf-8")


def send_telegram_alert(
    bot_token: str, chat_id: str, removed_events: list[tuple[str, dict]]
) -> None:
    lines = ["🚨 <b>Spotify Playlist Alert: Track(s) Removed!</b>\n"]
    for playlist_name, t in removed_events:
        artist = html.escape(t["artists"])
        name = html.escape(t["name"])
        pname = html.escape(playlist_name)
        if t.get("url"):
            url = html.escape(t["url"], quote=True)
            lines.append(
                f"• <b>{artist} — {name}</b> (<i>{pname}</i>)\n"
                f"  <a href=\"{url}\">Open in Spotify</a>"
            )
        else:
            lines.append(f"• <b>{artist} — {name}</b> (<i>{pname}</i>)")

    full_text = "\n".join(lines)
    chunks = [full_text[i : i + 3800] for i in range(0, len(full_text), 3800)]
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for chunk in chunks:
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        try:
            http_json(
                api_url,
                method="POST",
                headers={"Content-Type": "application/json"},
                data=payload,
            )
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            print(f"[Warning] Failed to send Telegram alert (HTTP {exc.code}): {err}")


def create_github_issue(removed_events: list[tuple[str, dict]], date_str: str) -> None:
    """Open a GitHub Issue listing removed tracks (triggers email notification)."""
    if os.environ.get("CREATE_GITHUB_ISSUE", "true").lower() == "false":
        return
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not gh_token or not gh_repo:
        return

    first_track = f"{removed_events[0][1]['artists']} — {removed_events[0][1]['name']}"
    if len(removed_events) == 1:
        title = f"🚨 Track removed: {first_track}"
    else:
        title = f"🚨 {len(removed_events)} tracks removed ({first_track}, ...)"

    body_lines = [
        f"During the scheduled check at `{date_str}`, the following track(s) were missing from your playlist:\n",
    ]
    for playlist_name, t in removed_events:
        link_part = f" — [Open in Spotify]({t['url']})" if t.get("url") else ""
        body_lines.append(f"- **{t['artists']} — {t['name']}** *(playlist: {playlist_name})*{link_part}")

    body_lines.append("\nYou can close this issue once you have reviewed or re-added the track(s).")

    payload = json.dumps({"title": title[:250], "body": "\n".join(body_lines)}).encode("utf-8")
    try:
        http_json(
            f"https://api.github.com/repos/{gh_repo}/issues",
            method="POST",
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            data=payload,
        )
        print("Created GitHub Issue for removed track(s).")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        print(f"[Warning] Failed to create GitHub Issue (HTTP {exc.code}): {err}")


def write_github_outputs(changed: bool, commit_msg: str) -> None:
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if not gh_output:
        return
    with open(gh_output, "a", encoding="utf-8") as fh:
        fh.write(f"changed={'true' if changed else 'false'}\n")
        fh.write(f"commit_msg={commit_msg}\n")


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
    raw_playlists = os.environ.get("SPOTIFY_PLAYLIST_ID", "").strip()

    missing = [
        name
        for name, val in [
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
            ("SPOTIFY_REFRESH_TOKEN", refresh_token),
            ("SPOTIFY_PLAYLIST_ID", raw_playlists),
        ]
        if not val
    ]
    if missing:
        print(f"Error: missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    playlist_ids = extract_playlist_ids(raw_playlists)
    if not playlist_ids:
        print("Error: could not parse any playlist IDs from SPOTIFY_PLAYLIST_ID.")
        sys.exit(1)

    access_token = get_access_token(client_id, client_secret, refresh_token)
    prev_snapshot = load_previous_snapshot()
    prev_playlists = prev_snapshot.get("playlists", {})

    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_playlists: dict[str, dict] = dict(prev_playlists)

    total_added = 0
    removed_events: list[tuple[str, dict]] = []
    any_changes = False
    is_initial_sync = False

    for pid in playlist_ids:
        pname = fetch_playlist_name(access_token, pid)
        curr_tracks = fetch_playlist_tracks(access_token, pid)

        prev_entry = prev_playlists.get(pid)
        prev_tracks: list[dict] = prev_entry.get("tracks", []) if isinstance(prev_entry, dict) else []

        # Safety guard against transient empty API responses wiping a populated snapshot
        if len(prev_tracks) >= 5 and len(curr_tracks) == 0:
            raise RuntimeError(
                f"Safety check triggered: playlist '{pname}' previously had {len(prev_tracks)} tracks, "
                "but the API returned 0. Aborting snapshot overwrite."
            )

        if prev_entry is None:
            is_initial_sync = True
            any_changes = True
            print(f"[{pname}] Initial sync: saved {len(curr_tracks)} tracks.")
        else:
            prev_map = {t["key"]: t for t in prev_tracks}
            curr_map = {t["key"]: t for t in curr_tracks}

            added_keys = [k for k in curr_map if k not in prev_map]
            removed_keys = [k for k in prev_map if k not in curr_map]

            if added_keys or removed_keys or prev_entry.get("name") != pname:
                any_changes = True

            total_added += len(added_keys)
            for k in added_keys:
                t = curr_map[k]
                print(f"[{pname}] + Added: {t['artists']} — {t['name']}")

            for k in removed_keys:
                t = prev_map[k]
                removed_events.append((pname, t))
                print(f"[{pname}] - REMOVED: {t['artists']} — {t['name']}")

            if not added_keys and not removed_keys:
                print(f"[{pname}] No changes ({len(curr_tracks)} tracks).")

        new_playlists[pid] = {
            "name": pname,
            "tracks": curr_tracks,
        }

    if any_changes:
        new_snapshot = {"playlists": new_playlists}
        save_snapshot(new_snapshot)
        write_playlist_markdown(new_snapshot, now_utc)

    if removed_events:
        append_removed_history(removed_events, now_utc)
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if tg_token and tg_chat:
            send_telegram_alert(tg_token, tg_chat, removed_events)
            print("Sent Telegram alert.")
        create_github_issue(removed_events, now_utc)

    if is_initial_sync and not removed_events:
        commit_msg = "Initial playlist snapshot"
    elif removed_events and total_added:
        commit_msg = f"Alert: removed {len(removed_events)}, added {total_added} track(s)"
    elif removed_events:
        commit_msg = f"Alert: removed {len(removed_events)} track(s)"
    elif total_added:
        commit_msg = f"Sync: added {total_added} track(s)"
    else:
        commit_msg = "Sync playlist metadata"

    write_github_outputs(any_changes, commit_msg)


if __name__ == "__main__":
    main()
