#!/usr/bin/env python3
"""Upload green-terminal showcase videos to YouTube.

Lists MP4 files in docs/img/, prompts multi-select, uploads each
selected video to the authenticated YouTube channel with optional
WebVTT subtitles.

Usage:
    python tools/upload_youtube.py

First run opens a browser for Google OAuth consent. You'll need:

  1. A Google Cloud project with the YouTube Data API v3 enabled
  2. An OAuth 2.0 Desktop application credential (client_secret.json)
  3. That file saved to ~/.config/green-terminal/client_secret.json

The OAuth token is cached in ~/.config/green-terminal/youtube-oauth.json
and automatically refreshed on subsequent runs.

Options:
    python tools/upload_youtube.py --no-auth    # dry-run: list + select only
    python tools/upload_youtube.py --dir /path  # scan a different directory
"""

import argparse
import ast
import pickle
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "img"
SIM_PY = REPO_ROOT / "tools" / "sim.py"
CONFIG_DIR = Path.home() / ".config" / "green-terminal"
TOKEN_PATH = CONFIG_DIR / "youtube-oauth.pkl"
CLIENT_SECRET_PATH = CONFIG_DIR / "client_secret.json"
SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


# ── Types ──────────────────────────────────────────────────────────────────
@dataclass
class VideoEntry:
    """A discovered video file with optional metadata."""
    path: Path
    stem: str
    size_kb: int
    has_subtitles: bool
    meta_title: Optional[str] = None
    meta_desc: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
# File discovery
# ══════════════════════════════════════════════════════════════════════════

def discover_videos(directory: Path = OUT_DIR) -> List[VideoEntry]:
    """Scan *directory* for ``*.mp4`` files and return a sorted list."""
    videos: List[VideoEntry] = []
    if not directory.is_dir():
        return videos

    for mp4 in sorted(directory.glob("*.mp4")):
        vtt = mp4.with_suffix(".vtt")
        entry = VideoEntry(
            path=mp4,
            stem=mp4.stem,
            size_kb=mp4.stat().st_size // 1024,
            has_subtitles=vtt.is_file(),
        )
        # Read embedded MP4 metadata tags (mutagen is optional)
        _try_read_mp4_meta(mp4, entry)
        videos.append(entry)

    return videos


def _try_read_mp4_meta(mp4_path: Path, entry: VideoEntry) -> None:
    """Attempt to read title/description from MP4 metadata tags."""
    try:
        from mutagen.mp4 import MP4 as MP4File
        m = MP4File(str(mp4_path))
        if "\xa9nam" in m:
            entry.meta_title = str(m["\xa9nam"][0])
        if "\xa9des" in m:
            entry.meta_desc = str(m["\xa9des"][0])
    except Exception:
        pass  # mutagen unavailable or file lacks tags


# ══════════════════════════════════════════════════════════════════════════
# Multi-select UI
# ══════════════════════════════════════════════════════════════════════════

def parse_selection(text: str, max_index: int) -> List[int]:
    """Parse a user selection string into 0-based indices.

    Supports:
        ``"1,3-5"`` → ``[0, 2, 3, 4]``
        ``"all"``   → ``[0, 1, …, max_index-1]``
    """
    text = text.strip().lower()
    if text == "all":
        return list(range(max_index))

    indices: set = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            continue
        start = int(m.group(1)) - 1  # 1‑based → 0‑based
        end = int(m.group(2)) if m.group(2) else start
        indices.update(i for i in range(start, min(end, max_index - 1) + 1) if i >= 0)
    return sorted(indices)


def prompt_multi_select(videos: List[VideoEntry]) -> List[VideoEntry]:
    """Show a numbered list and return user-selected entries."""
    if not videos:
        print("No videos found in docs/img/.")
        sys.exit(0)

    rel = OUT_DIR.relative_to(REPO_ROOT)
    print(f"\nVideos in {rel}/:\n")
    for i, v in enumerate(videos, 1):
        sub = "  + captions" if v.has_subtitles else ""
        print(f"  {i:>2}: {v.stem:<20} ({v.size_kb:>6} KiB){sub}")
    print()

    while True:
        raw = input("Select videos (e.g. 1,3-5 or 'all'): ").strip()
        if not raw:
            continue
        indices = parse_selection(raw, len(videos))
        if indices:
            return [videos[i] for i in indices]
        print("No valid selection. Try again.")


# ══════════════════════════════════════════════════════════════════════════
# Video title / description helpers
# ══════════════════════════════════════════════════════════════════════════

def _scenario_descriptions() -> dict:
    """Parse sim.py ``Scenario(…)`` calls for name → description mapping."""
    descs: dict = {}
    if not SIM_PY.is_file():
        return descs
    try:
        tree = ast.parse(SIM_PY.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Scenario":
                kwargs = {kw.arg: _ast_val(kw.value) for kw in node.keywords if kw.arg}
                name = kwargs.get("name")
                desc = kwargs.get("description")
                if name and desc:
                    descs[name] = desc
        return descs
    except Exception:
        return descs


def _ast_val(node) -> str:
    """Extract a string value from an AST node (name, attr, constant)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return "<unknown>"


def video_title(v: VideoEntry) -> str:
    """Return the best title for *v*: embedded metadata → filename."""
    return v.meta_title or v.stem


def video_description(v: VideoEntry) -> str:
    """Return the best description: embedded metadata → sim.py scenario."""
    if v.meta_desc:
        return v.meta_desc
    descs = _scenario_descriptions()
    return descs.get(v.stem, "")


# ══════════════════════════════════════════════════════════════════════════
# YouTube OAuth
# ══════════════════════════════════════════════════════════════════════════

def _load_cached_token() -> Any:
    """Load pickled credentials from the cache file, if it exists."""
    if not TOKEN_PATH.is_file():
        return None
    try:
        return pickle.loads(TOKEN_PATH.read_bytes())
    except Exception:
        return None


def _save_token(credentials) -> None:
    """Persist credentials to the cache file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_bytes(pickle.dumps(credentials))
    print(f"  ✓ OAuth token cached to {TOKEN_PATH}")


def get_authenticated_service():
    """Return an authenticated YouTube API v3 service handle.

    On first invocation this opens a browser for user consent.
    Subsequent runs reuse a cached token that is auto-refreshed.
    """
    credentials = _load_cached_token()

    # Refresh if expired (and a refresh token is available).
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception:
            credentials = None

    if not credentials or not credentials.valid:
        if not CLIENT_SECRET_PATH.is_file():
            print(
                f"Missing OAuth client secret.\n"
                f"1. Go to https://console.cloud.google.com/apis/credentials\n"
                f"2. Create an OAuth 2.0 Desktop application credential\n"
                f"3. Download the JSON and save it to:\n"
                f"   {CLIENT_SECRET_PATH}"
            )
            sys.exit(1)

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRET_PATH), SCOPE
        )
        credentials = flow.run_local_server(port=0)
        _save_token(credentials)

    return build("youtube", "v3", credentials=credentials)


# ══════════════════════════════════════════════════════════════════════════
# YouTube upload & captions
# ══════════════════════════════════════════════════════════════════════════

def upload_video(youtube, video: VideoEntry) -> Optional[str]:
    """Upload *video* to YouTube. Returns the new video ID, or None."""
    body = {
        "snippet": {
            "title": video_title(video),
            "description": video_description(video),
        },
        "status": {
            "privacyStatus": "public",
        },
    }
    media = MediaFileUpload(str(video.path), resumable=True)

    print(f"  uploading…", end="", flush=True)
    try:
        response = youtube.videos().insert(
            body=body, media_body=media, part="snippet,status"
        ).execute()
        video_id = response.get("id")
        print(f"  ✓ https://youtu.be/{video_id}")
        return video_id
    except Exception as exc:
        print(f"  ✗ {exc}")
        return None


def upload_captions(youtube, video_id: str, vtt_path: Path) -> bool:
    """Upload *vtt_path* as YouTube captions for *video_id*. Returns bool."""
    body = {
        "snippet": {
            "language": "en",
            "name": "",
            "isDraft": False,
        },
    }
    media = MediaFileUpload(str(vtt_path), mimetype="text/vtt")
    try:
        youtube.captions().insert(
            body=body, media_body=media, part="snippet", sync=False
        ).execute()
        print(f"    captions ✓")
        return True
    except Exception as exc:
        print(f"    captions ✗ {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", type=str, default=None,
                   help="Directory with MP4 files (default: docs/img/)")
    p.add_argument("--no-auth", action="store_true",
                   help="Dry-run: list and select, but don't upload")
    return p


def main() -> int:
    args = build_parser().parse_args()

    scan_dir = Path(args.dir) if args.dir else OUT_DIR
    videos = discover_videos(scan_dir)
    selected = prompt_multi_select(videos)

    if not selected:
        print("Nothing selected.")
        return 0

    # ── Dry-run ──────────────────────────────────────────────────────────
    if args.no_auth:
        print("\n[Dry-run — would upload:]\n")
        for v in selected:
            lines = [
                f"  {v.stem}",
                f"    title:       {video_title(v)}",
                f"    description: {video_description(v) or '(none)'}",
                f"    subtitles:   {'✓' if v.has_subtitles else '—'}",
            ]
            print("\n".join(lines))
        return 0

    # ── Real upload ───────────────────────────────────────────────────────
    print("\nAuthenticating with YouTube…")
    youtube = get_authenticated_service()
    print("  ✓ Authenticated")

    for v in selected:
        print(f"\n[{v.stem}]")
        vid = upload_video(youtube, v)
        if vid and v.has_subtitles:
            upload_captions(youtube, vid, v.path.with_suffix(".vtt"))

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
