# Media Fetcher

Private authenticated media retrieval service. FastAPI serves the API and static
frontend; a single asynchronous worker invokes the project-local yt-dlp CLI and
ffmpeg. Job and encrypted-profile metadata are in SQLite under `data/`.

## Safety invariants

- Keep the yt-dlp generic extractor disabled except for the trusted `b23.tv`
  redirector, whose sole purpose is resolving Bilibili short links.
- Never invoke yt-dlp or ffmpeg through a shell.
- Validate public HTTP(S) URLs before queueing work.
- Do not log full source URLs, cookies, proxy credentials, or decrypted profiles.
- Keep cookie filtering scoped to the selected site's domains.
- Files may only be served after checking both job ownership and containment in
  that job's download directory.
- Do not add DRM circumvention.

## Verification

```sh
.venv/bin/python -m pytest -q
curl -fsS http://127.0.0.1:8097/health
```
