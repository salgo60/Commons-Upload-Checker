#!/usr/bin/env python3
"""
Commons Upload Checker – Apple Photos Edition
Läser metadata direkt från Photos-biblioteket via osxphotos.
Exporterar original ENDAST för bilder som inte hittas på Wikimedia Commons.
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from tqdm import tqdm

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# ── Konstanter ────────────────────────────────────────────────────────────────
COMMONS_API   = "https://commons.wikimedia.org/w/api.php"
UA            = "CommonsUploadChecker/0.2 (salgo60@msn.com)"
RADIUS_M      = 100
API_DELAY     = 10.0
DEFAULT_CAT   = "Stockholm_Archipelago_Trail"
DEFAULT_USER  = "salgo60"
DB_DEFAULT    = Path("photos_checker.db")
CACHE_DEFAULT = Path(f"cache_{DEFAULT_CAT}.json")


# ── Databas ───────────────────────────────────────────────────────────────────
def init_db(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS assets (
            uuid              TEXT PRIMARY KEY,
            filename          TEXT,
            original_filename TEXT,
            date              TEXT,
            lat               REAL,
            lon               REAL,
            is_missing        INTEGER DEFAULT 0,
            status            TEXT    DEFAULT 'PENDING',
            commons_title     TEXT,
            commons_mid       TEXT,
            commons_url       TEXT,
            exported_path     TEXT,
            checked_at        TEXT,
            created_at        TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS run_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            args       TEXT,
            summary    TEXT
        );
    """)
    db.commit()


def upsert_asset(db: sqlite3.Connection, meta: dict) -> bool:
    """Infoga nytt asset. Returnerar True om det var nytt."""
    cur = db.execute(
        "SELECT uuid FROM assets WHERE uuid = ?", (meta["uuid"],)
    )
    if cur.fetchone():
        return False
    db.execute("""
        INSERT INTO assets (uuid, filename, original_filename, date, lat, lon, is_missing)
        VALUES (:uuid, :filename, :original_filename, :date, :lat, :lon, :is_missing)
    """, meta)
    db.commit()
    return True


def update_status(db: sqlite3.Connection, uuid: str, status: str,
                  commons_title: str = "", commons_mid: str = "",
                  commons_url: str = "", exported_path: str = "") -> None:
    db.execute("""
        UPDATE assets
        SET status = ?, commons_title = ?, commons_mid = ?,
            commons_url = ?, exported_path = ?, checked_at = datetime('now')
        WHERE uuid = ?
    """, (status, commons_title, commons_mid, commons_url, exported_path, uuid))
    db.commit()


# ── Apple Photos / osxphotos ──────────────────────────────────────────────────
def query_photos(from_date: str, to_date: str,
                 album: str | None = None) -> list[dict]:
    """Frågar Photos-biblioteket via osxphotos och returnerar raw JSON."""
    cmd = ["osxphotos", "query", "--json",
           "--from-date", from_date, "--to-date", to_date]
    if album:
        cmd += ["--album", album]
    print(f"Kör: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            print(f"osxphotos-fel: {result.stderr[:500]}", file=sys.stderr)
            return []
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print("osxphotos timeout – avbruten", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"JSON-fel: {e}", file=sys.stderr)
        return []


def extract_meta(asset: dict) -> dict | None:
    """Extraherar nödvändig metadata ur ett osxphotos-asset."""
    lat = asset.get("latitude")
    lon = asset.get("longitude")
    raw_date = asset.get("date")

    if not lat or not lon:
        return None  # Hoppa bilder utan GPS

    try:
        date_iso = datetime.fromisoformat(raw_date).strftime("%Y-%m-%d %H:%M:%S") if raw_date else None
    except (ValueError, TypeError):
        date_iso = None

    return {
        "uuid":              asset.get("uuid", ""),
        "filename":          asset.get("filename", ""),
        "original_filename": asset.get("original_filename") or asset.get("filename", ""),
        "date":              date_iso,
        "lat":               float(lat),
        "lon":               float(lon),
        "is_missing":        int(asset.get("ismissing", False)),
    }


def export_asset(uuid: str, output_dir: Path) -> Path | None:
    """Exporterar ett enskilt asset med osxphotos (triggar iCloud-nedladdning)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    before = set(output_dir.iterdir())
    cmd = ["osxphotos", "export", str(output_dir),
           "--uuid", uuid, "--original", "--skip-edited",
           "--convert-to-jpeg", "--jpeg-quality", "0.9",
           "--download-missing",  # triggar iCloud-nedladdning om nödvändigt
           "--no-progress"]
    print(f"  Exporterar {uuid} → {output_dir} ...", end=" ", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        after = set(output_dir.iterdir())
        new_files = after - before
        if new_files:
            newest = max(new_files, key=lambda p: p.stat().st_mtime)
            print(f"OK → {newest.name}")
            return newest
        print("ingen fil exporterad (iCloud ej tillgänglig?)")
    except subprocess.TimeoutExpired:
        print("timeout")
    return None


# ── Commons API ───────────────────────────────────────────────────────────────
def geosearch(lat: float, lon: float, radius: int = RADIUS_M) -> list[dict]:
    time.sleep(API_DELAY)
    params = {
        "action": "query", "list": "geosearch",
        "gscoord": f"{lat}|{lon}", "gsradius": radius,
        "gsnamespace": 6, "gslimit": 50, "format": "json",
    }
    try:
        r = requests.get(COMMONS_API, params=params, timeout=10, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.json().get("query", {}).get("geosearch", [])
    except Exception as e:
        tqdm.write(f"  [API-fel] {e}", file=sys.stderr)
        return []


DATE_TOLERANCE_DAYS = 30  # max dagars skillnad för username-match


def get_file_info(title: str) -> tuple[str | None, datetime | None]:
    """Hämtar uppladdare och datum från Commons imageinfo API."""
    params = {
        "action": "query", "titles": title,
        "prop": "imageinfo", "iiprop": "user|datetime", "format": "json",
    }
    try:
        r = requests.get(COMMONS_API, params=params, timeout=10, headers={"User-Agent": UA})
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            user = info.get("user")
            raw_date = info.get("timestamp", "")
            try:
                date = datetime.strptime(raw_date[:10], "%Y-%m-%d") if raw_date else None
            except ValueError:
                date = None
            return user, date
    except Exception:
        pass
    return None, None


def get_uploader(title: str) -> str | None:
    user, _ = get_file_info(title)
    return user


def get_sdc_date(pageid: int) -> datetime | None:
    try:
        r = requests.get(
            f"https://commons.wikimedia.org/entity/M{pageid}.json",
            timeout=10, headers={"User-Agent": UA})
        r.raise_for_status()
        for s in r.json().get("statements", {}).get("P571", []):
            raw = s["mainsnak"]["datavalue"]["value"]["time"]
            return datetime.strptime(raw[:11].lstrip("+"), "%Y-%m-%d")
    except Exception:
        pass
    return None


# ── Kategori-cache ────────────────────────────────────────────────────────────
def load_cache(cache_path: Path) -> dict[str, int]:
    """Laddar category cache {title: pageid} om den finns."""
    if not cache_path.exists():
        print(f"OBS: Kategori-cache {cache_path} saknas. "
              f"Kör checker.py --refresh-cache för att bygga den.", file=sys.stderr)
        return {}
    with cache_path.open(encoding="utf-8") as f:
        data = json.load(f)
    if "files" in data:
        return data["files"]
    return {t: 0 for t in data.get("titles", [])}


# ── Matchningslogik ───────────────────────────────────────────────────────────
def check_asset_against_commons(meta: dict, category_files: dict[str, int],
                                 username: str) -> tuple[str, str, str, str]:
    """
    Returnerar (status, commons_title, commons_mid, commons_url).
    Status: FOUND | NOT_FOUND | AMBIGUOUS
    """
    candidates = geosearch(meta["lat"], meta["lon"])
    if category_files:
        candidates = [c for c in candidates if c["title"] in category_files]

    if not candidates:
        return "NOT_FOUND", "", "", ""

    for c in candidates:
        title = c["title"]
        pageid = category_files.get(title, 0)
        mid = f"M{pageid}" if pageid else ""
        url = "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")

        uploader, upload_date = get_file_info(title)
        if uploader and uploader.lower() == username.lower():
            # Kontrollera datum även för username-match (undviker falskt FOUND)
            photo_date = None
            try:
                photo_date = datetime.strptime(meta["date"][:10], "%Y-%m-%d") if meta.get("date") else None
            except ValueError:
                pass
            if photo_date and upload_date:
                diff = abs((photo_date - upload_date).days)
                if diff <= DATE_TOLERANCE_DAYS:
                    return "FOUND", title, mid, url
                # Stor datumskillnad trots rätt uppladdare → inte samma bild
                continue
            else:
                # Inget datum att jämföra – acceptera username-match
                return "FOUND", title, mid, url

    # Träff finns men inte av rätt uppladdare
    return "AMBIGUOUS", candidates[0]["title"], "", \
           "https://commons.wikimedia.org/wiki/" + candidates[0]["title"].replace(" ", "_")


# ── HTML-rapport ──────────────────────────────────────────────────────────────
def write_report(db: sqlite3.Connection, path: Path, category: str,
                 session_uuids: list[str] | None = None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if session_uuids:
        placeholders = ",".join("?" * len(session_uuids))
        rows = db.execute(f"""
            SELECT uuid, original_filename, date, lat, lon,
                   status, commons_title, commons_mid, commons_url, exported_path
            FROM assets WHERE uuid IN ({placeholders}) ORDER BY date
        """, session_uuids).fetchall()
    else:
        rows = db.execute("""
            SELECT uuid, original_filename, date, lat, lon,
                   status, commons_title, commons_mid, commons_url, exported_path
            FROM assets ORDER BY date
        """).fetchall()

    def osm(lat, lon):
        return f"https://www.openstreetmap.org/#map=19/{lat}/{lon}&layers=N"
    def wikimap(lat, lon):
        return f"https://wikimap.toolforge.org/?wp=false&basemap=2&cluster=false&zoom=18&lat={lat}&lon={lon}"
    def upload_url():
        return "https://commons.wikimedia.org/wiki/Special:UploadWizard"

    counts = {}
    rows_html = ""
    for r in rows:
        uuid, fname, date, lat, lon, status, c_title, c_mid, c_url, exp_path = r
        counts[status] = counts.get(status, 0) + 1
        cls = "found" if status == "FOUND" else ("ambig" if status == "AMBIGUOUS" else "notfound")

        # Filnamn + UUID-knapp (kopierar UUID → sök i Photos)
        short_uuid = uuid[:8] if uuid else "?"
        photos_btn = (f'<button class="uuid-btn" onclick="copyUUID(\'{uuid}\')" '
                      f'title="Kopiera UUID – sök i Photos: {uuid}">📷 {short_uuid}…</button>')
        file_cell = f'{fname or "?"}<br>{photos_btn}'

        coord_cell = "–"
        if lat and lon:
            coord_cell = (
                f'<a href="{wikimap(lat,lon)}" target="_blank" title="WikiMap">📍 WikiMap</a>'
                f'&nbsp;<a href="{osm(lat,lon)}" target="_blank" title="OpenStreetMap">🗺 OSM</a>'
            )

        action_cell = "–"
        if status == "FOUND" and c_url:
            mid_link = f' <a href="https://commons.wikimedia.org/entity/{c_mid}" target="_blank">{c_mid}</a>' if c_mid else ""
            action_cell = f'<a href="{c_url}" target="_blank">🖼 {c_title}</a>{mid_link}'
        elif status == "AMBIGUOUS" and c_url:
            action_cell = f'⚠️ <a href="{c_url}" target="_blank">{c_title}</a>'
        elif status == "NOT_FOUND":
            exp = f' ✅ <code>{exp_path}</code>' if exp_path else f' <a href="{upload_url()}" target="_blank">⬆️ Ladda upp</a>'
            action_cell = exp

        rows_html += f"""
        <tr class="{cls}">
          <td>{file_cell}</td>
          <td>{date or "–"}</td>
          <td>{coord_cell}</td>
          <td>{status}</td>
          <td>{action_cell}</td>
        </tr>"""

    summary = " &nbsp;|&nbsp; ".join(f"<b>{v}</b> {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    cat_url = f"https://commons.wikimedia.org/wiki/Category:{category.replace(' ', '_')}"

    html = f"""<!DOCTYPE html>
<html lang="sv"><head><meta charset="UTF-8">
<title>Photos → Commons – {now}</title>
<style>
  body{{font-family:system-ui,sans-serif;padding:1rem 2rem}}
  h1{{color:#3366cc}}
  .summary{{background:#f0f4ff;padding:.7rem 1rem;border-radius:6px;margin-bottom:1rem}}
  table{{border-collapse:collapse;width:100%;font-size:.9rem}}
  th{{background:#3366cc;color:#fff;padding:.5rem .8rem;text-align:left;
      position:sticky;top:0;cursor:pointer;user-select:none}}
  th:hover{{background:#2255bb}}
  th.asc::after{{content:" ▲"}}
  th.desc::after{{content:" ▼"}}
  td{{padding:.4rem .8rem;border-bottom:1px solid #ddd;vertical-align:top}}
  tr.found td:first-child{{border-left:4px solid #2da44e}}
  tr.ambig td:first-child{{border-left:4px solid #f0a500}}
  tr.notfound td:first-child{{border-left:4px solid #cf222e}}
  tr:hover{{background:#f6f8fa}}
  a{{color:#3366cc;text-decoration:none}}
  a:hover{{text-decoration:underline}}
  .uuid-btn{{font-size:.75rem;padding:2px 6px;border:1px solid #ccc;
             border-radius:4px;background:#f6f8fa;cursor:pointer;color:#555}}
  .uuid-btn:hover{{background:#e0e7ff}}
  #toast{{position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);
          background:#333;color:#fff;padding:.5rem 1.2rem;border-radius:6px;
          display:none;font-size:.9rem;z-index:999}}
</style></head><body>
<h1>Apple Photos → Wikimedia Commons</h1>
<p class="summary">
  Kördes: {now} &nbsp;|&nbsp; {len(rows)} bilder kontrollerade<br>
  Kategori: <a href="{cat_url}" target="_blank">{category}</a><br>
  {summary}
</p>
<div id="toast">UUID kopierat! Sök i Photos med ⌘F</div>
<table id="report">
  <thead><tr>
    <th onclick="sortTable(0)">Fil / UUID</th>
    <th onclick="sortTable(1)">Datum</th>
    <th onclick="sortTable(2)">Koordinater</th>
    <th onclick="sortTable(3)">Status</th>
    <th>Commons / Åtgärd</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<script>
function copyUUID(uuid) {{
  navigator.clipboard.writeText(uuid).then(() => {{
    var t = document.getElementById('toast');
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 2000);
  }});
}}
function sortTable(col) {{
  var tbl = document.getElementById('report');
  var th = tbl.querySelectorAll('th')[col];
  var asc = !th.classList.contains('asc');
  tbl.querySelectorAll('th').forEach(h => h.classList.remove('asc','desc'));
  th.classList.add(asc ? 'asc' : 'desc');
  var rows = Array.from(tbl.tBodies[0].rows);
  rows.sort(function(a, b) {{
    var x = a.cells[col].innerText.trim();
    var y = b.cells[col].innerText.trim();
    return asc ? x.localeCompare(y, 'sv') : y.localeCompare(x, 'sv');
  }});
  rows.forEach(r => tbl.tBodies[0].appendChild(r));
}}
</script>
</body></html>"""

    path.write_text(html, encoding="utf-8")
    print(f"HTML-rapport sparad i {path}")


# ── Ta bort FOUND-bilder från Photos-album (rebuild-approach) ─────────────────
def remove_found_from_album(db: sqlite3.Connection, album_name: str,
                            session_uuids: list[str]) -> None:
    """Bygger om albumet utan FOUND-bilder.
    Photos.app AppleScript stödjer inte 'remove from album' direkt,
    så vi skapar ett nytt album med bara NOT_FOUND/AMBIGUOUS, raderar gamla.
    """
    if not session_uuids:
        print("\n4. Inga assets att bearbeta.")
        return

    placeholders = ",".join("?" * len(session_uuids))
    found_uuids = {r[0] for r in db.execute(
        f"SELECT uuid FROM assets WHERE status='FOUND' AND uuid IN ({placeholders})",
        session_uuids
    )}
    keep_uuids = [u for u in session_uuids if u not in found_uuids]

    print(f"\n4. Bygger om albumet '{album_name}': "
          f"{len(found_uuids)} FOUND tas bort, {len(keep_uuids)} behålls ...")

    if not found_uuids:
        print("   Inga FOUND att ta bort.")
        return

    temp_name = f"{album_name}_uppdaterad"

    # Skapa temp-album
    script_create = f'tell application "Photos" to make new album named "{temp_name}"'
    subprocess.run(["osascript", "-e", script_create], capture_output=True, timeout=20)

    # Lägg till keep-bilder i temp-albumet (batchar om 20)
    added = 0
    for i in range(0, len(keep_uuids), 20):
        batch = keep_uuids[i : i + 20]
        uuid_list = ", ".join(f'"{u}"' for u in batch)
        script = f'''
tell application "Photos"
    set newAlbum to album named "{temp_name}"
    set toAdd to {{}}
    repeat with u in {{{uuid_list}}}
        try
            set end of toAdd to media item id (contents of u)
        end try
    end repeat
    add toAdd to newAlbum
    return count of toAdd
end tell'''
        for attempt in range(3):
            try:
                r = subprocess.run(["osascript", "-e", script],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    added += len(batch)
                    break
                time.sleep(2)
            except Exception:
                time.sleep(2)
        print(f"   {added}/{len(keep_uuids)} kopierade ...", end="\r", flush=True)
        time.sleep(0.3)

    # Radera gamla albumet och döp om
    script_rename = f'''
tell application "Photos"
    delete album named "{album_name}"
    set name of album named "{temp_name}" to "{album_name}"
end tell'''
    r = subprocess.run(["osascript", "-e", script_rename],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        print(f"\n   Klart! Album '{album_name}' nu med {len(keep_uuids)} bilder "
              f"({len(found_uuids)} FOUND borttagna).")
    else:
        print(f"\n   OBS: Rename misslyckades: {r.stderr.strip()}")
        print(f"   Albumet '{temp_name}' skapades med {added} bilder – radera '{album_name}' manuellt.")



def main() -> None:
    global API_DELAY
    parser = argparse.ArgumentParser(
        description="Kontrollera Apple Photos mot Wikimedia Commons via osxphotos."
    )
    parser.add_argument("--from-date", required=True, help="Startdatum YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="Slutdatum YYYY-MM-DD")
    parser.add_argument("--album",     help="Albumnamn i Photos")
    parser.add_argument("-u", "--username", default=DEFAULT_USER,
                        help=f"Commons-uppladdare (standard: {DEFAULT_USER})")
    parser.add_argument("-c", "--category", default=DEFAULT_CAT,
                        help=f"Commons-kategori (standard: {DEFAULT_CAT})")
    parser.add_argument("--cache", type=Path, default=CACHE_DEFAULT,
                        help="Sökväg till kategori-cache JSON")
    parser.add_argument("--db",    type=Path, default=DB_DEFAULT,
                        help="SQLite-databas för resultat")
    parser.add_argument("--export-dir", type=Path, default=Path("exports"),
                        help="Mapp för exporterade NOT_FOUND-bilder")
    parser.add_argument("--html",  type=Path,
                        default=Path(f"rapport_photos_{datetime.now().strftime('%Y%m%d')}.html"),
                        help="HTML-rapport (standard: rapport_photos_YYYYMMDD.html)")
    parser.add_argument("--delay", type=float, default=API_DELAY,
                        help=f"Sekunder mellan API-anrop (standard: {API_DELAY})")
    parser.add_argument("--no-export", action="store_true",
                        help="Kontrollera men exportera inte NOT_FOUND-bilder")
    parser.add_argument("--remove-found", action="store_true",
                        help="Ta bort FOUND-bilder från Photos-albumet efter kontroll")
    parser.add_argument("--recheck", action="store_true",
                        help="Kör om Commons-kontroll för PENDING + AMBIGUOUS")
    args = parser.parse_args()

    API_DELAY = args.delay

    # Databas
    db = sqlite3.connect(args.db)
    init_db(db)

    # Kategori-cache
    category_files = load_cache(args.cache)
    if not category_files:
        print("Ingen kategori-cache – söker i hela Commons (långsammare).")

    # Hämta Photos-metadata
    # osxphotos --to-date är exklusivt ("before DATE"), lägg till 1 dag
    from datetime import timedelta
    to_date_exclusive = (datetime.strptime(args.to_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"\n1. Hämtar metadata från Apple Photos ({args.from_date} – {args.to_date})...")
    assets_raw = query_photos(args.from_date, to_date_exclusive, args.album)
    print(f"   {len(assets_raw)} assets hittade i Photos.")

    # Håll koll på UUIDs från aktuell session (för remove-found)
    session_uuids = [a["uuid"] for a in assets_raw if a.get("uuid")]

    new_count = 0
    skipped_no_gps = 0
    for asset in assets_raw:
        meta = extract_meta(asset)
        if meta is None:
            skipped_no_gps += 1
            continue
        if upsert_asset(db, meta):
            new_count += 1

    print(f"   Nya assets i DB: {new_count} | Utan GPS (hoppas): {skipped_no_gps}")

    # Hämta PENDING (och AMBIGUOUS om --recheck)
    statuses = ("PENDING", "AMBIGUOUS") if args.recheck else ("PENDING",)
    placeholders = ",".join("?" * len(statuses))
    pending = db.execute(
        f"SELECT uuid, filename, date, lat, lon FROM assets WHERE status IN ({placeholders})",
        statuses
    ).fetchall()

    if not pending:
        print("\n2. Inga PENDING assets – allt redan kontrollerat.")
    else:
        eta = len(pending) * args.delay / 60
        print(f"\n2. Kontrollerar {len(pending)} assets mot Commons (~{eta:.0f} min)...")
        with tqdm(pending, unit="bild", dynamic_ncols=True) as bar:
            for uuid, fname, date, lat, lon in bar:
                bar.set_description(fname[:28] if fname else uuid[:12])
                meta = {"uuid": uuid, "filename": fname, "date": date,
                        "lat": lat, "lon": lon}
                status, c_title, c_mid, c_url = check_asset_against_commons(
                    meta, category_files, args.username)
                update_status(db, uuid, status, c_title, c_mid, c_url)
                bar.set_postfix(status=status)

    # Export av NOT_FOUND
    if not args.no_export:
        not_found = db.execute(
            "SELECT uuid, original_filename FROM assets WHERE status = 'NOT_FOUND' AND exported_path IS NULL OR exported_path = ''"
        ).fetchall()
        if not_found:
            print(f"\n3. Exporterar {len(not_found)} NOT_FOUND-bilder till {args.export_dir}...")
            for uuid, fname in tqdm(not_found, unit="fil"):
                exp_path = export_asset(uuid, args.export_dir)
                if exp_path:
                    update_status(db, uuid, "NOT_FOUND", exported_path=str(exp_path))
        else:
            print("\n3. Inga NOT_FOUND att exportera.")

    # Sammanfattning (enbart aktuell session)
    print("\n─── Sammanfattning (aktuell session) ───")
    if session_uuids:
        placeholders = ",".join("?" * len(session_uuids))
        for row in db.execute(
            f"SELECT status, COUNT(*) FROM assets WHERE uuid IN ({placeholders}) GROUP BY status ORDER BY COUNT(*) DESC",
            session_uuids
        ):
            print(f"  {row[0]}: {row[1]}")
    else:
        print("  (inga assets)")

    # HTML-rapport (enbart aktuell session)
    write_report(db, args.html, args.category, session_uuids)

    # Ta bort FOUND från album
    if args.remove_found and args.album:
        remove_found_from_album(db, args.album, session_uuids)
    elif args.remove_found and not args.album:
        print("\n4. --remove-found kräver --album, hoppas över.")

    db.close()


if __name__ == "__main__":
    main()
