# YouTube Upload Tool — Design

Upload showcase videos from `docs/img/` to the `@NotesFromTheInquisitor` YouTube
channel, including companion subtitle files.

## Tool

A standalone Python script at `tools/upload_youtube.py`.

## Flow

1. Scan `docs/img/` for `*.mp4` files.
2. Present a numbered multi-select menu:
   ```
    1: basic.mp4     (2.3 MB)  + subtitles
    2: long.mp4      (4.1 MB)
    3: alert.mp4     (1.8 MB)  + subtitles
   Select videos (e.g. 1,3-5 or "all"): _
   ```
3. On first run, open a browser for Google OAuth 2.0 consent.
   Token saved to `~/.config/green-terminal/youtube-oauth.json`.
4. For each selected video:
   - Read embedded MP4 metadata for title/description.
   - If no embedded title: use filename stem (e.g. `basic.mp4` → `"basic"`).
   - If no embedded description: match filename to `tools/sim.py`
     `Scenario.description`.
   - Upload via YouTube Data API v3 (resumable, H.264).
   - Privacy status: `public`.
   - If companion `<stem>.vtt` exists, upload it as a YouTube caption
     (language `en`, `sync=false`).
   - Report success or error per video; continue on error.

## Dependencies

- `google-api-python-client` — YouTube API
- `google-auth-oauthlib` — OAuth 2.0 browser flow

## OAuth

- Google Cloud Project: YouTube Data API v3 enabled.
- OAuth 2.0 Desktop credentials → download `client_secret.json`.
- Scope: `https://www.googleapis.com/auth/youtube.upload`.
- First run: `client_secret.json` read from `~/.config/green-terminal/`.
- Token persisted to `~/.config/green-terminal/youtube-oauth.json`.

## Metadata Priority

| Field        | Source 1 (preferred)      | Source 2 (fallback)              |
|-------------|---------------------------|----------------------------------|
| Title       | MP4 `title` metadata tag  | Filename stem                    |
| Description | MP4 `description` tag     | sim.py `Scenario.description`    |

## Subtitles

- Format: WebVTT (`.vtt`)
- Companion file naming: `<video_stem>.vtt` (e.g. `basic.vtt` for `basic.mp4`)
- Uploaded via `youtube.captions().insert()`
