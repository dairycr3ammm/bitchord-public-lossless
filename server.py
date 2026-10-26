import base64
import json
import os
import re
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, jsonify, request

app = Flask(__name__)
PORT = int(os.environ.get("PORT", "8080"))
ADDON_ID = os.environ.get("ADDON_ID", "community.public-audio.lossless")
ADDON_NAME = os.environ.get("ADDON_NAME", "Public Audio Lossless")
USER_AGENT = "BitChord-Public-Lossless-Addon/1.0 (personal-use; contact: admin@example.invalid)"
IA_ADVANCED = "https://archive.org/advancedsearch.php"
IA_METADATA = "https://archive.org/metadata/"

# Only return items with an explicit public-domain or Creative Commons license.
# This is a conservative filter, not a legal determination of every item's status.
ALLOWED_LICENSE_MARKERS = (
    "publicdomain",
    "creativecommons.org/publicdomain/",
    "creativecommons.org/licenses/by/",
    "creativecommons.org/licenses/by-sa/",
    "creativecommons.org/licenses/by-nd/",
    "creativecommons.org/licenses/by-nc/",
    "creativecommons.org/licenses/by-nc-sa/",
    "creativecommons.org/licenses/by-nc-nd/",
)

def http_json(url, timeout=15):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))

def allowed_license(value):
    license_text = str(value or "").strip().lower()
    return bool(license_text) and any(marker in license_text for marker in ALLOWED_LICENSE_MARKERS)

def encode_track(identifier, filename):
    raw = json.dumps({"identifier": identifier, "filename": filename}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def decode_track(track_id):
    padding = "=" * ((4 - len(track_id) % 4) % 4)
    raw = base64.urlsafe_b64decode((track_id + padding).encode())
    data = json.loads(raw.decode())
    identifier = str(data["identifier"])
    filename = str(data["filename"])
    if not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
        raise ValueError("Invalid archive identifier")
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise ValueError("Invalid filename")
    return identifier, filename

def fetch_item(identifier):
    return http_json(IA_METADATA + quote(identifier, safe=""))


def flac_files(metadata):
    candidates = []
    for item in metadata.get("files", []):
        name = str(item.get("name", "")).strip()

        # Only accept actual FLAC files.
        if name.lower().endswith(".flac"):
            candidates.append(item)

    def rank(item):
        sample_rate = parse_int(
            item.get("sample_rate")
            or item.get("samplerate")
            or item.get("sampleRate")
        )
        bit_depth = parse_int(
            item.get("bit_depth")
            or item.get("bitdepth")
            or item.get("bitDepth")
        )
        return (
            sample_rate or 0,
            bit_depth or 0,
            parse_int(item.get("size")) or 0
        )

    return sorted(candidates, key=rank, reverse=True)

def parse_int(value):
    try:
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None
    except Exception:
        return None

def get_license(metadata):
    meta = metadata.get("metadata", {})
    return meta.get("licenseurl") or meta.get("license_url") or meta.get("license")

def item_title(metadata, fallback):
    meta = metadata.get("metadata", {})
    return str(meta.get("title") or fallback or "Unknown title")

def item_creator(metadata):
    meta = metadata.get("metadata", {})
    creator = meta.get("creator", "")
    if isinstance(creator, list):
        creator = ", ".join(str(x) for x in creator[:3])
    return str(creator or "Unknown artist")

def item_duration(file_info, metadata):
    for value in (file_info.get("length"), metadata.get("metadata", {}).get("length")):
        try:
            if value is not None:
                return float(str(value).split()[0])
        except (ValueError, TypeError):
            pass
    return None

def direct_url(identifier, filename):
    return "https://archive.org/download/{}/{}".format(
        quote(identifier, safe=""), quote(filename, safe="")
    )

@app.get("/")
def root():
    return jsonify({
        "service": ADDON_NAME,
        "manifest": "/manifest.json",
        "health": "/health"
    })

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": ADDON_NAME})

@app.get("/manifest.json")
def manifest():
    return jsonify({
        "id": ADDON_ID,
        "name": ADDON_NAME,
        "version": "1.0.0",
        "description": "Searches Internet Archive for explicitly public-domain or Creative Commons licensed FLAC audio. Hi-Res metadata is reported only when the source supplies it.",
        "resources": ["search", "stream"],
        "allowDownloads": 1,
        "checkValidLossless": 0,
        "settings": [
            {
                "key": "quality",
                "type": "select",
                "default": "lossless",
                "options": [
                    {"label": "Lossless / prefer highest available FLAC", "value": "lossless"},
                    {"label": "High", "value": "high"},
                    {"label": "Low", "value": "low"}
                ]
            }
        ]
    })

def track_title_from_filename(filename):
    name = filename.rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0]
    name = re.sub(r"(?i)^onclassical_demo_", "", name)
    name = name.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name.title() if name else "Unknown track"

@app.get("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})
    if len(query) > 160:
        query = query[:160]
    # Escape characters that have special meaning in the Archive search query language.
    safe_query = query.replace("\\", " ").replace('"', " ").replace("(", " ").replace(")", " ")
    q = 'mediatype:audio AND (title:"{0}" OR creator:"{0}")'.format(safe_query)
    params = {
        "q": q,
        "fl[]": ["identifier", "title", "creator", "licenseurl"],
        "rows": "12",
        "page": "1",
        "output": "json",
        "sort[]": "downloads desc"
    }
    # urllib's standard urlencode handles repeated fields when doseq=True.
    url = IA_ADVANCED + "?" + urlencode(params, doseq=True)
    try:
        results = http_json(url, timeout=20).get("response", {}).get("docs", [])
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return jsonify({"tracks": [], "error": "Archive search is temporarily unavailable"}), 502

    tracks = []
    for result in results:
        identifier = str(result.get("identifier") or "")
        if not identifier:
            continue
        try:
            item = fetch_item(identifier)
        except Exception:
            continue
        license_value = get_license(item) or result.get("licenseurl")
        if not allowed_license(license_value):
            continue
        files = flac_files(item)
        if not files:
            continue
        # Include FLAC files only; sort prefers explicit high-res metadata when supplied.
        for file_info in files[:2]:
            filename = str(file_info.get("name", ""))
            if not filename:
                continue
            meta = item.get("metadata", {})
            track = {
    "id": encode_track(identifier, filename),
    "title": str(file_info.get("title") or track_title_from_filename(filename)),
    "artist": item_creator(item),
    "album": str(meta.get("album") or item_title(item, result.get("title"))),
    "format": "flac",
    "audioQuality": "LOSSLESS",
    "duration": item_duration(file_info, item),
    "artworkURL": "https://archive.org/services/img/" + quote(identifier, safe=""),
}

            # Do not invent sample rate or bit depth. Report only explicit source metadata.
            sr = parse_int(file_info.get("sample_rate") or file_info.get("samplerate") or file_info.get("sampleRate"))
            bd = parse_int(file_info.get("bit_depth") or file_info.get("bitdepth") or file_info.get("bitDepth"))
            if sr:
                track["sampleRate"] = sr
            if bd:
                track["bitDepth"] = bd
            tracks.append({k: v for k, v in track.items() if v is not None})
    return jsonify({"tracks": tracks[:20]})

@app.get("/stream/<track_id>")
def stream(track_id):
    try:
        identifier, filename = decode_track(track_id)
        item = fetch_item(identifier)
    except Exception:
        return jsonify({"error": "Unknown or invalid track ID"}), 404

    if not allowed_license(get_license(item)):
        return jsonify({"error": "This item does not have an accepted public license"}), 404

    valid_files = {str(f.get("name")) for f in flac_files(item)}
    if filename not in valid_files:
        return jsonify({"error": "FLAC file not found"}), 404

    file_info = next(f for f in item.get("files", []) if str(f.get("name")) == filename)
    result = {
        "url": direct_url(identifier, filename),
        "format": "flac",
        "quality": "Lossless FLAC",
        "codec": "flac",
        "container": "flac",
        "manifest": "none",
        "encrypted": False,
    }
    sr = parse_int(file_info.get("sample_rate") or file_info.get("samplerate") or file_info.get("sampleRate"))
    bd = parse_int(file_info.get("bit_depth") or file_info.get("bitdepth") or file_info.get("bitDepth"))
    bitrate = parse_int(file_info.get("bitrate"))
    if sr:
        result["sampleRate"] = sr
    if bd:
        result["bitDepth"] = bd
    if bitrate:
        result["bitrate"] = bitrate
    if sr and bd and (sr > 44100 or bd > 16):
        result["quality"] = "Hi-Res Lossless FLAC"
    return jsonify(result)

@app.errorhandler(429)
def rate_limited(_error):
    return jsonify({"error": "Rate limited; try again shortly"}), 429, {"Retry-After": "30"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
