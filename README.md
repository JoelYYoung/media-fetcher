# Media Fetcher

Authenticated personal service for retrieving permitted audio or video from
YouTube, Bilibili, and the explicit site extractors shipped by yt-dlp.

## Features

- Original audio, high-quality MP3, compatible 720p/1080p MP4, or best video.
- Single items and playlists/collections capped at 50 entries.
- Optional Chinese and English subtitles for video jobs. Available tracks are
  embedded in the video and also kept as individually downloadable SRT files;
  playlist subtitles are included in the result ZIP.
- Single-concurrency background queue with live progress and cancellation.
- One-click speech-to-text for a completed single audio/video file, backed by
  the local Voice Studio Whisper model. Long media is normalized and split into
  Voice-safe segments automatically; text can be viewed, copied, or downloaded.
- Per-user YouTube/Bilibili cookies encrypted with a key held in macOS Keychain.
- Per-site HTTP(S)/SOCKS proxy profiles with credentials hidden after saving.
- Shared account database and job ownership checks.
- Public-address validation, no shell execution, and generic extractor disabled
  except when resolving trusted `b23.tv` short links.
- 24-hour expiry and a 50 GiB global generated-file quota by default.

The service does not bypass DRM. Only retrieve content you own or have permission
to save. Automated access or downloading may also be restricted by a site's terms.

## Setup

```sh
brew install ffmpeg deno
./scripts/setup.sh
./scripts/serve.sh
```

Local port: `8097`.

Configuration lives in `.env`; see `.env.example`. Runtime state and downloads
live under `data/` and are excluded from Git.

Set `VIBETYPST_DB` to a compatible SQLite account database containing `users`
and `sessions` tables. Authentication is deliberately kept separate from the
media retrieval core.

## Speech to text

After a single-file job completes, choose **语音转文字** on its result card.
Media Fetcher extracts mono 16 kHz audio, splits long inputs into approximately
13-minute WAV segments under Voice Studio's 30 MB upload limit, and submits them
to Voice Studio's existing single-model queue at `http://127.0.0.1:8092`.

The optional integration requires a local Voice Studio service. It uses a
short-lived shared login session that is deleted when the transcription finishes.
It does not store a Voice password or API token.
Playlist ZIP files are intentionally excluded; fetch a single playlist item when
a transcript is needed. Override the local endpoint with `VOICE_API_URL`.

## Login cookies

Public media is always attempted anonymously first. A matching encrypted Cookie
profile is only used automatically if anonymous probing fails. For content that
requires a login, import a Netscape-format `cookies.txt` from the
Login Configuration dialog. The importer discards all domains except the selected
site.

For YouTube, use a dedicated non-primary account. Export only `youtube.com`
cookies from a one-time private/incognito session, then close that session. Account
cookies can trigger platform rate limits or account enforcement; keep concurrency
low and only configure them when needed.

## Operations

```sh
curl -fsS http://127.0.0.1:8097/health
tail -f data/service-error.log
```

## Secret handling

- `.env`, `data/`, databases, logs, downloads, `cookies.txt`, and local
  LaunchAgent files are ignored by Git.
- Imported YouTube/Bilibili cookies and proxy credentials are domain-filtered
  and encrypted. On macOS, the encryption key is generated in Keychain.
- For non-macOS development, set `MEDIA_FETCHER_FERNET_KEY` locally; never
  commit it.
- Review the site's terms and only retrieve content you are allowed to save.

Run tests with `.venv/bin/python -m pytest -q`.
