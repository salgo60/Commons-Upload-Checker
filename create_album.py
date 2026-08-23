#!/usr/bin/env python3
"""
Skapar ett Apple Photos-album med alla bilder från ett datumintervall.
Albumet är sedan redo för manuell granskning innan photos_checker.py körs.

Använder osascript (AppleScript via CLI) direkt – undviker photoscript timeout.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta


def query_uuids(from_date: str, to_date: str) -> list[str]:
    """Hämtar UUID för alla bilder i datumintervallet via osxphotos."""
    cmd = ["osxphotos", "query", "--json",
           "--from-date", from_date, "--to-date", to_date]
    print(f"Hämtar bilder {from_date} → {to_date} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            print(f"osxphotos-fel: {result.stderr[:300]}", file=sys.stderr)
            return []
        assets = json.loads(result.stdout)
        uuids = [a["uuid"] for a in assets if a.get("uuid")]
        print(f"  → {len(uuids)} bilder hittade.")
        return uuids
    except Exception as e:
        print(f"Fel: {e}", file=sys.stderr)
        return []


def run_applescript(script: str, timeout: int = 60) -> str:
    """Kör ett AppleScript via osascript och returnerar stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def ensure_album(album_name: str) -> None:
    """Skapar albumet om det inte redan finns."""
    script = f'''
tell application "Photos"
    if not (exists album named "{album_name}") then
        make new album named "{album_name}"
        return "created"
    else
        return "exists"
    end if
end tell'''
    status = run_applescript(script, timeout=30)
    if status == "created":
        print(f"Album '{album_name}' skapat.")
    else:
        print(f"Album '{album_name}' finns redan – lägger till bilder.")


def add_uuids_to_album(album_name: str, uuids: list[str]) -> None:
    """Lägger till bilder (via UUID) i albumet, 20 åt gången."""
    batch_size = 20
    added = 0
    errors = 0
    total = len(uuids)

    for i in range(0, total, batch_size):
        batch = uuids[i : i + batch_size]
        # Bygg AppleScript-lista med UUID:n
        uuid_list = ", ".join(f'"{u}"' for u in batch)
        script = f'''
tell application "Photos"
    set theAlbum to album named "{album_name}"
    set theItems to {{}}
    repeat with u in {{{uuid_list}}}
        try
            set end of theItems to media item id u
        end try
    end repeat
    add theItems to theAlbum
    return count of theItems
end tell'''
        for attempt in range(3):
            try:
                n = int(run_applescript(script, timeout=30))
                added += n
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(3)
                else:
                    errors += len(batch)
        print(f"  {added}/{total} tillagda ...", end="\r", flush=True)
        time.sleep(0.3)

    print(f"\nKlart! {added} bilder tillagda, {errors} misslyckades.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Skapar ett Photos-album med bilder för manuell granskning."
    )
    parser.add_argument("--from-date", required=True, help="Startdatum YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="Slutdatum YYYY-MM-DD")
    parser.add_argument("--album",
                        help="Albumnamn (standard: 'Commons-Kandidater YYYY')")
    args = parser.parse_args()

    year = args.from_date[:4]
    album_name = args.album or f"Commons-Kandidater {year}"

    # osxphotos --to-date är exklusivt ("before DATE"), lägg till 1 dag
    to_date_inclusive = (
        datetime.strptime(args.to_date, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    uuids = query_uuids(args.from_date, to_date_inclusive)
    if not uuids:
        print("Inga bilder hittades.")
        sys.exit(0)

    ensure_album(album_name)
    add_uuids_to_album(album_name, uuids)

    print(f"\nÖppna Photos → Album → '{album_name}'")
    print("Gå igenom manuellt, ta bort bilder du inte vill ladda upp.")
    print(f'\nSedan kör du:')
    print(f'  python photos_checker.py --from-date {args.from_date} --to-date {args.to_date} --album "{album_name}"')


if __name__ == "__main__":
    main()
