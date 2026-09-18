import os
import sys
import json
import time
import hashlib
import traceback
from pathlib import Path
from datetime import datetime, timezone

import requests
from google.auth.transport.requests import Request

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


# ============================================================
# CONFIG
# ============================================================

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

VIZARD_CREATE = (
    "https://elb-api.vizard.ai/"
    "hvizard-server-front/open-api/v1/project/create"
)

VIZARD_QUERY = (
    "https://elb-api.vizard.ai/"
    "hvizard-server-front/open-api/v1/project/query/{}"
)

TELEGRAM_API = "https://api.telegram.org/bot{}/{}"

MRBEAST_CHANNEL_ID = os.environ.get(
    "MRBEAST_CHANNEL_ID",
    "UCX6OQ3DkcsbYNE6H8uQQuVA"
)

# Minimum source length.
# 60 seconds is enough because Vizard creates the Short.
MIN_SOURCE_SECONDS = 60

# We ask YouTube for reasonably fresh/new videos first.
SEARCH_PAGES = 4

# Number of Vizard results requested.
MAX_VIZARD_CLIPS = 10

# Vizard processing timeout.
VIZARD_TIMEOUT_SECONDS = 30 * 60

# Poll every 30 seconds.
VIZARD_POLL_SECONDS = 30

# Don't choose extremely tiny clips.
MIN_CLIP_SECONDS = 20

# YouTube Shorts target.
TARGET_TITLE_MAX = 95


# ============================================================
# FILES
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

USED_VIDEOS_FILE = DATA_DIR / "used_videos.txt"
USED_CLIPS_FILE = DATA_DIR / "used_clips.txt"
UPLOAD_HISTORY_FILE = DATA_DIR / "upload_history.json"


# ============================================================
# ENV / SECRETS
# ============================================================

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()

VIZARD_API_KEY = os.environ.get("VIZARD_API_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# HTTP SESSION
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "YouTube-Auto-Shorts/1.0"
})


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_text():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")


def require_env():
    required = {
        "YOUTUBE_API_KEY": YOUTUBE_API_KEY,
        "YOUTUBE_CLIENT_ID": YOUTUBE_CLIENT_ID,
        "YOUTUBE_CLIENT_SECRET": YOUTUBE_CLIENT_SECRET,
        "YOUTUBE_REFRESH_TOKEN": YOUTUBE_REFRESH_TOKEN,
        "VIZARD_API_KEY": VIZARD_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [
        key for key, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing GitHub Secrets: " + ", ".join(missing)
        )


def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {
            "raw": response.text[:2000]
        }


def request_with_retry(
    method,
    url,
    *,
    attempts=4,
    timeout=60,
    **kwargs
):
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.request(
                method,
                url,
                timeout=timeout,
                **kwargs
            )

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                if attempt < attempts:
                    time.sleep(min(5 * attempt, 20))
                    continue

            return response

        except requests.RequestException as exc:
            last_error = exc

            if attempt < attempts:
                time.sleep(min(5 * attempt, 20))
                continue

    raise RuntimeError(
        f"Request failed after {attempts} attempts: {last_error}"
    )


def parse_iso_duration(value):
    """
    Converts ISO-8601 YouTube duration:
    PT1H2M3S
    PT12M30S
    PT45S
    into seconds.
    """
    if not value:
        return 0

    value = value.upper()

    hours = 0
    minutes = 0
    seconds = 0

    number = ""

    for char in value:
        if char.isdigit():
            number += char
            continue

        if char == "H":
            hours = int(number or 0)
            number = ""

        elif char == "M":
            minutes = int(number or 0)
            number = ""

        elif char == "S":
            seconds = int(number or 0)
            number = ""

    return hours * 3600 + minutes * 60 + seconds


def format_seconds(seconds):
    seconds = int(seconds or 0)

    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"

    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"

    return f"{seconds}s"


def shorten(text, limit=900):
    text = str(text or "").strip()

    if len(text) <= limit:
        return text

    return text[:limit - 3] + "..."


def make_fingerprint(*parts):
    raw = "||".join(
        str(part or "").strip()
        for part in parts
    )

    return hashlib.sha256(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = TELEGRAM_API.format(
        TELEGRAM_BOT_TOKEN,
        "sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }

    try:
        response = request_with_retry(
            "POST",
            url,
            json=payload,
            timeout=30
        )

        if not response.ok:
            print(
                "Telegram error:",
                response.status_code,
                response.text[:500]
            )

    except Exception as exc:
        print("Telegram notification failed:", exc)


# ============================================================
# HISTORY
# ============================================================

def load_lines(path):
    if not path.exists():
        return set()

    result = set()

    for line in path.read_text(
        encoding="utf-8",
        errors="ignore"
    ).splitlines():

        line = line.strip()

        if line:
            result.add(line)

    return result


def save_lines(path, values):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    path.write_text(
        "\n".join(sorted(values)) + "\n",
        encoding="utf-8"
    )


def load_history():
    if not UPLOAD_HISTORY_FILE.exists():
        return []

    try:
        data = json.loads(
            UPLOAD_HISTORY_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, list):
            return data

    except Exception:
        pass

    return []


def save_history(history):
    UPLOAD_HISTORY_FILE.write_text(
        json.dumps(
            history[-500:],
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


USED_VIDEOS = load_lines(USED_VIDEOS_FILE)
USED_CLIPS = load_lines(USED_CLIPS_FILE)
UPLOAD_HISTORY = load_history()


# ============================================================
# YOUTUBE SEARCH
# ============================================================

def youtube_get(endpoint, params):
    url = f"{YOUTUBE_API}/{endpoint}"

    params = dict(params)
    params["key"] = YOUTUBE_API_KEY

    response = request_with_retry(
        "GET",
        url,
        params=params,
        timeout=45
    )

    data = safe_json(response)

    if not response.ok:
        raise RuntimeError(
            "YouTube API error: "
            + shorten(json.dumps(data), 1200)
        )

    return data


def search_mrbeast_videos():
    """
    Search only inside the official MrBeast channel.

    We intentionally DO NOT use Creative Commons filtering here
    because this automation assumes the channel owner has granted
    the required reuse permission.
    """

    candidates = []
    seen_ids = set()

    # Different queries give YouTube multiple chances to return
    # useful videos instead of relying on one exact query.
    queries = [
        "",
        "MrBeast",
        "challenge",
        "competition",
        "survival",
        "money",
        "beast games",
        "challenge",
    ]

    for query in queries:

        page_token = None

        for _ in range(SEARCH_PAGES):

            params = {
                "part": "snippet",
                "channelId": MRBEAST_CHANNEL_ID,
                "type": "video",
                "order": "date",
                "maxResults": 50,
            }

            if query:
                params["q"] = query

            if page_token:
                params["pageToken"] = page_token

            data = youtube_get(
                "search",
                params
            )

            for item in data.get("items", []):

                video_id = (
                    item.get("id", {})
                    .get("videoId")
                )

                if not video_id:
                    continue

                if video_id in seen_ids:
                    continue

                seen_ids.add(video_id)

                snippet = item.get(
                    "snippet",
                    {}
                )

                candidates.append({
                    "id": video_id,
                    "title": snippet.get(
                        "title",
                        "Untitled"
                    ),
                    "description": snippet.get(
                        "description",
                        ""
                    ),
                    "published": snippet.get(
                        "publishedAt",
                        ""
                    ),
                    "channel_id": snippet.get(
                        "channelId",
                        ""
                    ),
                    "channel_title": snippet.get(
                        "channelTitle",
                        "MrBeast"
                    )
                })

            page_token = data.get(
                "nextPageToken"
            )

            if not page_token:
                break

            # Avoid excessive API calls.
            if len(candidates) >= 100:
                break

        if len(candidates) >= 100:
            break

    return candidates


def get_video_details(video_ids):
    if not video_ids:
        return []

    result = []

    # YouTube allows up to 50 IDs in one request.
    for start in range(0, len(video_ids), 50):

        chunk = video_ids[start:start + 50]

        data = youtube_get(
            "videos",
            {
                "part": "snippet,contentDetails,statistics,status",
                "id": ",".join(chunk)
            }
        )

        result.extend(
            data.get("items", [])
        )

    return result


def choose_source():
    print("Searching official MrBeast channel...")

    search_results = search_mrbeast_videos()

    if not search_results:
        raise RuntimeError(
            "No videos found on the configured MrBeast channel."
        )

    ids = [
        item["id"]
        for item in search_results
    ]

    details = get_video_details(ids)

    candidates = []

    for item in details:

        video_id = item.get("id")

        if not video_id:
            continue

        # HARD source-level duplicate protection.
        if video_id in USED_VIDEOS:
            continue

        content = item.get(
            "contentDetails",
            {}
        )

        duration = parse_iso_duration(
            content.get("duration")
        )

        if duration < MIN_SOURCE_SECONDS:
            continue

        status = item.get(
            "status",
            {}
        )

        # Skip deleted/private/unavailable sources.
        if status.get("uploadStatus") not in (
            None,
            "processed",
        ):
            continue

        snippet = item.get(
            "snippet",
            {}
        )

        stats = item.get(
            "statistics",
            {}
        )

        try:
            views = int(
                stats.get(
                    "viewCount",
                    0
                )
            )
        except Exception:
            views = 0

        try:
            likes = int(
                stats.get(
                    "likeCount",
                    0
                )
            )
        except Exception:
            likes = 0

        title = snippet.get(
            "title",
            "MrBeast"
        )

        candidates.append({
            "id": video_id,
            "title": title,
            "description": snippet.get(
                "description",
                ""
            ),
            "duration": duration,
            "views": views,
            "likes": likes,
            "published": snippet.get(
                "publishedAt",
                ""
            ),
            "channel_title": snippet.get(
                "channelTitle",
                "MrBeast"
            ),
            "url": f"https://www.youtube.com/watch?v={video_id}"
        })

    if not candidates:
        return None

    # Prefer videos with strong view count while still giving
    # newer videos a chance.
    #
    # This is not a guarantee of "viral"; it is just a source
    # selection heuristic.
    candidates.sort(
        key=lambda x: (
            x["views"],
            x["likes"],
            x["published"]
        ),
        reverse=True
    )

    return candidates[0]


# ============================================================
# VIZARD
# ============================================================

def vizard_submit(source):
    payload = {
        "lang": "auto",

        # 30-60 second output
        "preferLength": [2],

        "videoUrl": source["url"],

        # 2 = YouTube
        "videoType": 2,

        # 1 = 9:16 vertical
        "ratioOfClip": 1,

        # Return multiple candidates.
        "maxClipNumber": MAX_VIZARD_CLIPS,

        # Keep original audio; these only affect editing.
        "subtitleSwitch": 1,
        "headlineSwitch": 1,
        "removeSilenceSwitch": 1,

        "clipModel": "clip_v1",

        "projectName": (
            "MrBeast Auto Short "
            + source["id"]
        )
    }

    headers = {
        "VIZARDAI_API_KEY": VIZARD_API_KEY,
        "Content-Type": "application/json"
    }

    response = request_with_retry(
        "POST",
        VIZARD_CREATE,
        headers=headers,
        json=payload,
        timeout=90
    )

    data = safe_json(response)

    if not response.ok:
        raise RuntimeError(
            "Vizard submit HTTP error: "
            + shorten(
                json.dumps(data),
                1500
            )
        )

    print(
        "Vizard submit response:",
        json.dumps(data)[:3000]
    )

    # Handle common response layouts.
    project_id = (
        data.get("projectId")
        or data.get("data", {}).get("projectId")
        or data.get("result", {}).get("projectId")
    )

    if not project_id:
        raise RuntimeError(
            "Vizard did not return projectId: "
            + shorten(
                json.dumps(data),
                1800
            )
        )

    return str(project_id)


def vizard_query(project_id):
    headers = {
        "VIZARDAI_API_KEY": VIZARD_API_KEY
    }

    response = request_with_retry(
        "GET",
        VIZARD_QUERY.format(project_id),
        headers=headers,
        timeout=60
    )

    data = safe_json(response)

    if not response.ok:
        raise RuntimeError(
            "Vizard query HTTP error: "
            + shorten(
                json.dumps(data),
                1500
            )
        )

    return data


def vizard_wait(project_id):
    started = time.time()

    while True:

        if (
            time.time() - started
            > VIZARD_TIMEOUT_SECONDS
        ):
            raise RuntimeError(
                "Vizard processing timed out after "
                f"{VIZARD_TIMEOUT_SECONDS // 60} minutes."
            )

        data = vizard_query(
            project_id
        )

        code = data.get("code")

        print(
            "Vizard status:",
            code
        )

        if code == 2000:

            videos = data.get(
                "videos",
                []
            )

            if not videos:
                raise RuntimeError(
                    "Vizard returned success but no clips."
                )

            return videos

        if code not in (
            None,
            1000
        ):
            raise RuntimeError(
                "Vizard processing failed: "
                + shorten(
                    json.dumps(data),
                    1800
                )
            )

        time.sleep(
            VIZARD_POLL_SECONDS
        )


# ============================================================
# CLIP DUPLICATE PROTECTION
# ============================================================

def clip_fingerprint(source, clip):
    """
    Creates a persistent fingerprint.

    Vizard videoId is primary.

    Transcript/title/duration/source are included as a second
    layer so a regenerated clip can still be recognized.
    """

    vizard_id = str(
        clip.get(
            "videoId",
            ""
        )
    )

    transcript = clip.get(
        "transcript",
        ""
    )

    title = clip.get(
        "title",
        ""
    )

    duration = clip.get(
        "videoMsDuration",
        ""
    )

    return make_fingerprint(
        source["id"],
        vizard_id,
        title,
        duration,
        transcript[:1500]
    )


def choose_clip(source, clips):
    available = []

    for clip in clips:

        url = clip.get(
            "videoUrl"
        )

        if not url:
            continue

        duration_ms = clip.get(
            "videoMsDuration",
            0
        )

        try:
            duration = float(
                duration_ms
            ) / 1000
        except Exception:
            duration = 0

        if duration < MIN_CLIP_SECONDS:
            continue

        fingerprint = clip_fingerprint(
            source,
            clip
        )

        # Primary duplicate check.
        if fingerprint in USED_CLIPS:
            continue

        # Vizard's own ID is also stored independently.
        vizard_id = str(
            clip.get(
                "videoId",
                ""
            )
        )

        if vizard_id and (
            "VIZARD:" + vizard_id
        ) in USED_CLIPS:
            continue

        try:
            score = float(
                clip.get(
                    "viralScore",
                    0
                )
            )
        except Exception:
            score = 0

        available.append({
            "clip": clip,
            "fingerprint": fingerprint,
            "score": score,
            "duration": duration,
            "vizard_id": vizard_id
        })

    if not available:
        return None

    # Vizard documents that generated clips are sorted by viral
    # score. We still explicitly sort so our selection is clear.
    available.sort(
        key=lambda x: (
            x["score"],
            x["duration"]
        ),
        reverse=True
    )

    return available[0]


# ============================================================
# DOWNLOAD
# ============================================================

def download_clip(url, output_path):
    print(
        "Downloading generated clip..."
    )

    with request_with_retry(
        "GET",
        url,
        stream=True,
        timeout=90
    ) as response:

        if not response.ok:
            raise RuntimeError(
                "Failed to download Vizard clip: "
                f"HTTP {response.status_code}"
            )

        with open(
            output_path,
            "wb"
        ) as file:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    file.write(chunk)

    size = output_path.stat().st_size

    if size < 10_000:
        raise RuntimeError(
            "Downloaded clip appears invalid."
        )

    print(
        "Downloaded:",
        size,
        "bytes"
    )


# ============================================================
# YOUTUBE OAUTH
# ============================================================

def youtube_service():
    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload"
        ]
    )

    # Refresh automatically when the client needs a token.
    credentials.refresh(Request())

    return build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )


# ============================================================
# TITLE / DESCRIPTION
# ============================================================

def build_title(source, clip):
    title = str(
        clip.get(
            "title",
            ""
        )
    ).strip()

    if not title:
        title = source["title"]

    # Avoid absurdly long upload title.
    title = title.replace(
        "\n",
        " "
    ).strip()

    if len(title) > TARGET_TITLE_MAX:
        title = (
            title[:TARGET_TITLE_MAX - 3]
            + "..."
        )

    return title


def build_description(source, clip):
    score = clip.get(
        "viralScore",
        "N/A"
    )

    reason = clip.get(
        "viralReason",
        ""
    )

    return f"""This Short was created from authorized source content.

Original source:
{source["title"]}

Source:
{source["url"]}

Channel:
{source["channel_title"]}

Clip score:
{score}/10

Vizard reason:
{reason}

The source content is used under permission from the content owner.

#shorts #MrBeast #viral
"""


# ============================================================
# YOUTUBE UPLOAD
# ============================================================

def upload_to_youtube(
    video_file,
    source,
    clip
):
    print(
        "Uploading Short to YouTube..."
    )

    youtube = youtube_service()

    title = build_title(
        source,
        clip
    )

    description = build_description(
        source,
        clip
    )

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [
                "shorts",
                "MrBeast",
                "viral",
                "short video"
            ],
            "categoryId": "22"
        },

        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(
        str(video_file),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None

    last_error = None

    for attempt in range(1, 4):

        try:
            while response is None:

                status, response = (
                    request.next_chunk()
                )

                if status:
                    print(
                        "Upload progress:",
                        int(
                            status.progress() * 100
                        ),
                        "%"
                    )

            break

        except Exception as exc:

            last_error = exc

            print(
                "Upload attempt failed:",
                attempt,
                exc
            )

            if attempt < 3:
                time.sleep(
                    10 * attempt
                )

                # Re-create the upload request.
                media = MediaFileUpload(
                    str(video_file),
                    mimetype="video/mp4",
                    resumable=True,
                    chunksize=8 * 1024 * 1024
                )

                request = youtube.videos().insert(
                    part="snippet,status",
                    body=body,
                    media_body=media
                )

                response = None

    if response is None:
        raise RuntimeError(
            "YouTube upload failed: "
            + str(last_error)
        )

    video_id = response.get(
        "id"
    )

    if not video_id:
        raise RuntimeError(
            "YouTube upload returned no video ID."
        )

    return video_id, title


# ============================================================
# MAIN
# ============================================================

def main():

    require_env()

    print("=" * 70)
    print("YOUTUBE AUTO SHORTS")
    print("Time:", now_text())
    print("=" * 70)

    telegram(
        "🤖 YouTube Automation STARTED\n\n"
        "🔎 Searching the official MrBeast channel "
        "for a NEW source...\n\n"
        "♻️ Duplicate source + duplicate clip protection: ON"
    )

    source = None
    video_file = Path("generated_short.mp4")

    try:

        # ----------------------------------------------------
        # SOURCE
        # ----------------------------------------------------

        source = choose_source()

        if not source:

            message = (
                "⚠️ YouTube Automation SKIPPED\n\n"
                "No unused eligible MrBeast source video "
                "was found in this run.\n\n"
                "♻️ Duplicate protection prevented "
                "reusing an old source."
            )

            telegram(message)

            print(message)

            return 0

        telegram(
            "🎬 NEW SOURCE FOUND\n\n"
            f"Title: {shorten(source['title'], 250)}\n"
            f"Duration: {format_seconds(source['duration'])}\n"
            f"Views: {source['views']:,}\n"
            f"URL: {source['url']}"
        )

        print(
            "Selected source:",
            source["title"]
        )

        # ----------------------------------------------------
        # VIZARD
        # ----------------------------------------------------

        telegram(
            "✂️ Sending source to Vizard...\n\n"
            "Target: 30–60 sec\n"
            "Format: 9:16\n"
            "Original clip audio: ON"
        )

        project_id = vizard_submit(
            source
        )

        print(
            "Vizard project:",
            project_id
        )

        clips = vizard_wait(
            project_id
        )

        print(
            "Vizard returned",
            len(clips),
            "clips."
        )

        # ----------------------------------------------------
        # CHOOSE UNUSED CLIP
        # ----------------------------------------------------

        selected = choose_clip(
            source,
            clips
        )

        if not selected:

            message = (
                "⚠️ YouTube Automation SKIPPED\n\n"
                "Vizard returned clips, but all eligible "
                "clips were already present in automation "
                "history.\n\n"
                "♻️ No duplicate clip was uploaded."
            )

            telegram(message)

            print(message)

            return 0

        clip = selected["clip"]

        print(
            "Selected Vizard clip:",
            selected["vizard_id"]
        )

        print(
            "Viral score:",
            selected["score"]
        )

        telegram(
            "🔥 CLIP SELECTED\n\n"
            f"Viral score: {selected['score']}/10\n"
            f"Duration: {int(selected['duration'])} sec\n"
            f"Vizard ID: {selected['vizard_id']}"
        )

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        download_clip(
            clip["videoUrl"],
            video_file
        )

        # ----------------------------------------------------
        # UPLOAD
        # ----------------------------------------------------

        youtube_video_id, upload_title = (
            upload_to_youtube(
                video_file,
                source,
                clip
            )
        )

        youtube_url = (
            "https://www.youtube.com/watch?v="
            + youtube_video_id
        )

        # ----------------------------------------------------
        # ONLY MARK USED AFTER SUCCESSFUL UPLOAD
        # ----------------------------------------------------

        USED_VIDEOS.add(
            source["id"]
        )

        USED_CLIPS.add(
            selected["fingerprint"]
        )

        if selected["vizard_id"]:
            USED_CLIPS.add(
                "VIZARD:"
                + selected["vizard_id"]
            )

        save_lines(
            USED_VIDEOS_FILE,
            USED_VIDEOS
        )

        save_lines(
            USED_CLIPS_FILE,
            USED_CLIPS
        )

        # ----------------------------------------------------
        # SAVE DETAILED HISTORY
        # ----------------------------------------------------

        UPLOAD_HISTORY.append({
            "uploaded_at_utc": now_utc().isoformat(),
            "source_video_id": source["id"],
            "source_title": source["title"],
            "source_url": source["url"],
            "source_duration_seconds": source["duration"],
            "vizard_project_id": project_id,
            "vizard_clip_id": selected["vizard_id"],
            "clip_fingerprint": selected["fingerprint"],
            "clip_duration_seconds": selected["duration"],
            "viral_score": selected["score"],
            "youtube_video_id": youtube_video_id,
            "youtube_url": youtube_url,
            "youtube_title": upload_title
        })

        save_history(
            UPLOAD_HISTORY
        )

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        telegram(
            "✅ YouTube Automation SUCCESS\n\n"
            f"🎬 {shorten(upload_title, 250)}\n\n"
            f"🔥 Viral score: {selected['score']}/10\n"
            f"⏱ Clip: {int(selected['duration'])} sec\n\n"
            f"▶️ {youtube_url}\n\n"
            "♻️ Source marked as USED.\n"
            "♻️ Clip marked as USED.\n"
            "🚫 This source/clip will not be uploaded again."
        )

        print(
            "SUCCESS:",
            youtube_url
        )

        return 0

    except Exception as exc:

        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "AUTOMATION ERROR:",
            error_text
        )

        traceback.print_exc()

        telegram(
            "🚨 YouTube Automation ERROR\n\n"
            + shorten(
                error_text,
                2500
            )
            + "\n\n"
            "❌ No duplicate fallback upload was attempted."
        )

        return 1

    finally:

        try:
            if video_file.exists():
                video_file.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(
        main()
    )
