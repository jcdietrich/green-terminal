# YouTube Upload Tool — Implementation Plan

> **For agentic workers:** Implementation is a single self-contained task. Execute inline.

**Goal:** Create `tools/upload_youtube.py` — a CLI tool that lists MP4s in `docs/img/`, lets the user multi-select, and uploads each to YouTube with subtitles.

**Architecture:** Single Python script with functions for: file scanning → multi-select → OAuth auth → video upload → caption upload. Core logic (file discovery, multi-select parsing, metadata reading) is separated from YouTube API calls.

**Tech Stack:** Python 3, `google-api-python-client`, `google-auth-oauthlib`, `mutagen` (for MP4 metadata), standard library only otherwise.

---
### Task 1: Create `tools/upload_youtube.py`

**Files:**
- Create: `tools/upload_youtube.py`

#### Step 1: Write the script

```python
#!/usr/bin/env python3
"""Upload green-terminal showcase videos to YouTube.

Lists MP4 files in docs/img/, prompts multi-select, uploads each
selected video to the authenticated YouTube channel with optional
WebVTT subtitles.

Usage:
    python tools/upload_youtube.py

First run opens a browser for Google OAuth consent.
Token is cached in ~/.config/green-terminal/youtube-oauth.json.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# ── Dirs ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "docs" / "img"
SIM_PY = REPO_ROOT / "tools" / "sim.py"
CONFIG_DIR = Path.home() / ".config" / "green-terminal"
TOKEN_PATH = CONFIG_DIR / "youtube-oauth.json"
CLIENT_SECRET_PATH = CONFIG_DIR / "client_secret.json"

SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]

# ── Video entry ───────────────────────────────────────────────────────────
@dataclass
class VideoEntry:
    path: Path               # full path to .mp4
    stem: str                # filename without extension
    size_kb: int             # file size in KiB
    has_subtitles: bool      # companion .vtt exists?
    meta_title: Optional[str] = None    # from MP4 metadata
    meta_desc: Optional[str] = None     # from MP4 metadata

# ── File discovery ────────────────────────────────────────────────────────
def discover_videos(directory: Path = OUT_DIR) -> List[VideoEntry]:
    """Scan directory for .mp4 files and return sorted VideoEntry list."""
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
        # Try reading embedded MP4 metadata
        try:
            from mutagen.mp4 import MP4
            m = MP4(str(mp4))
            if "\xa9nam" in m:
                entry.meta_title = str(m["\xa9nam"][0])
            if "\xa9des" in m:
                entry.meta_desc = str(m["\xa9des"][0])
        except (ImportError, Exception):
            pass  # mutagen not installed or metadata unreadable
        videos.append(entry)
    return videos


# ── Multi-select UI ───────────────────────────────────────────────────────
def parse_selection(text: str, max_index: int) -> List[int]:
    """Parse user selection string like '1,3-5' into 0-based indices.

    Supports: individual numbers, ranges (3-5), and 'all'.
    Returns sorted unique 0-based indices.
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
        start = int(m.group(1)) - 1  # convert to 0-based
        if m.group(2):
            end = int(m.group(2))
            indices.update(range(max(start, 0), min(end, max_index + 1)))
        else:
            if 0 <= start < max_index:
                indices.add(start)
    return sorted(indices)


def prompt_multi_select(videos: List[VideoEntry]) -> List[VideoEntry]:
    """Show numbered list and prompt for selection. Returns selected entries."""
    if not videos:
        print("No videos found in docs/img/.")
        sys.exit(0)

    print(f"\nVideos in {OUT_DIR.relative_to(REPO_ROOT)}/:\n")
    for i, v in enumerate(videos, 1):
        sub = "  + subtitles" if v.has_subtitles else ""
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


# ── Metadata helpers ──────────────────────────────────────────────────────
def _scenario_descriptions() -> dict:
    """Parse sim.py Scenario definitions to map name→description."""
    descs: dict = {}
    if not SIM_PY.is_file():
        return descs
    # Pattern: name="basic", description="..."
    import ast
    try:
        source = SIM_PY.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'Scenario':
                kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
                if 'name' in kwargs and 'description' in kwargs:
                    name = kwargs['name'].value if isinstance(kwargs['name'], ast.Constant) else None
                    desc = kwargs['description'].value if isinstance(kwargs['description'], ast.Constant) else None
                    if name and desc:
                        descs[name] = desc
        return descs
    except Exception:
        return descs


def video_title(v: VideoEntry) -> str:
    """Determine video title: embedded metadata > filename stem."""
    if v.meta_title:
        return v.meta_title
    return v.stem


def video_description(v: VideoEntry) -> str:
    """Determine video description: embedded metadata > sim.py scenario."""
    if v.meta_desc:
        return v.meta_desc
    descs = _scenario_descriptions()
    return descs.get(v.stem, "")


# ── OAuth ──────────────────────────────────────────────────────────────────
def get_authenticated_service():
    """Get authenticated YouTube service via OAuth 2.0.

    On first run, opens browser for consent. Token cached for reuse.
    Requires client_secret.json in ~/.config/green-terminal/.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    import pickle

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    credentials = None

    # Try loading cached token
    if TOKEN_PATH.is_file():
        try:
            import pickle
            credentials = pickle.loads(TOKEN_PATH.read_bytes())
        except Exception:
            pass

    # Refresh if expired
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception:
            credentials = None

    # New auth flow
    if not credentials or not credentials.valid:
        if not CLIENT_SECRET_PATH.is_file():
            print(
                f"Missing OAuth client secret.\n"
                f"Download your Desktop OAuth 2.0 client_secret.json from\n"
                f"Google Cloud Console → APIs & Services → Credentials\n"
                f"and save it to:\n  {CLIENT_SECRET_PATH}"
            )
            sys.exit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPE)
        credentials = flow.run_local_server(port=0)
        # Cache token
        import pickle
        TOKEN_PATH.write_bytes(pickle.dumps(credentials))
        print(f"  ✓ OAuth token cached to {TOKEN_PATH}")

    return build("youtube", "v3", credentials=credentials)


# ── YouTube upload ────────────────────────────────────────────────────────
def upload_video(youtube, video: VideoEntry) -> Optional[str]:
    """Upload a single video to YouTube. Returns video ID on success."""
    from googleapiclient.http import MediaFileUpload

    title = video_title(video)
    description = video_description(video)

    body = {
        "snippet": {
            "title": title,
            "description": description,
        },
        "status": {
            "privacyStatus": "public",
        },
    }

    media = MediaFileUpload(str(video.path), resumable=True)

    print(f"  Uploading {video.stem}...", end="", flush=True)
    try:
        request = youtube.videos().insert(
            body=body,
            media_body=media,
            part="snippet,status",
        )
        response = request.execute()
        video_id = response.get("id")
        print(f" ✓ (youtu.be/{video_id})")
        return video_id
    except Exception as e:
        print(f" ✗ {e}")
        return None


def upload_captions(youtube, video_id: str, vtt_path: Path) -> bool:
    """Upload .vtt file as YouTube captions. Returns True on success."""
    from googleapiclient.http import MediaFileUpload

    if not vtt_path.is_file():
        return False

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
            body=body,
            media_body=media,
            part="snippet",
            sync=False,
        ).execute()
        print(f"    captions: ✓")
        return True
    except Exception as e:
        print(f"    captions: ✗ {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=str, default=None,
                        help="Directory with MP4 files (default: docs/img/)")
    parser.add_argument("--no-auth", action="store_true",
                        help="Skip upload — just list and select (dry-run)")
    args = parser.parse_args()

    scan_dir = Path(args.dir) if args.dir else OUT_DIR
    videos = discover_videos(scan_dir)
    selected = prompt_multi_select(videos)

    if not selected:
        print("Nothing selected.")
        return 0

    if args.no_auth:
        print("\n[Dry-run mode — would upload:]")
        for v in selected:
            title = video_title(v)
            desc = video_description(v)
            print(f"  {v.stem}")
            print(f"    title:       {title}")
            print(f"    description: {desc or '(none)'}")
            print(f"    subtitles:   {'✓' if v.has_subtitles else '—'}")
        return 0

    print(f"\nAuthenticating with YouTube...")
    youtube = get_authenticated_service()
    print(f"  ✓ Authenticated")

    for v in selected:
        print(f"\n[{v.stem}]")
        vid = upload_video(youtube, v)
        if vid and v.has_subtitles:
            upload_captions(youtube, vid, v.path.with_suffix(".vtt"))

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

#### Step 2: Install dependencies and verify

```bash
pip install google-api-python-client google-auth-oauthlib mutagen
```

#### Step 3: Run the dry-run to verify listing/selection works

```bash
python tools/upload_youtube.py --no-auth
```

#### Step 4: Make it executable

```bash
chmod +x tools/upload_youtube.py
```

#### Step 5: Commit

```bash
git add tools/upload_youtube.py
git commit -m "feat: add YouTube upload tool for docs/img showcase videos"
```

---
