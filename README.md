# Edison

A silent macOS menu-bar app that records your Microsoft Teams calls (your microphone and the other participants separately), then transcribes, identifies speakers, summarises and archives them. Everything runs locally; no audio or text leaves your Mac.

> **Consent:** recording calls may require the consent of all participants (GDPR and local law). Make sure you have it.

## Requirements

- Apple Silicon Mac, macOS 15+
- Xcode Command Line Tools (`xcode-select --install`)
- Python 3.11+ (`brew install python`)
- ~15 GB free disk (models) and 16 GB+ RAM recommended

## Install

```bash
./install.sh
```

It creates the Python environment, downloads the models (Whisper large-v3, Qwen2.5-14B, speaker models), creates a local signing identity (`make_cert.sh`), builds `~/Applications/Edison.app`, and starts it. Safe to re-run (also use it to update after changing the code).

On first run macOS asks for **Microphone** and **Screen & System Audio Recording** access. Both are required: the second is how the other participants' audio is captured.

Remove with `./uninstall.sh` (recordings and voice database are kept).

## Using it

Edison detects when Teams starts using the microphone and records automatically. Menu-bar icon:

| Item | What it does |
| --- | --- |
| Open Edison… | Opens the manager window |
| Open recordings folder | Opens `~/Teams Recordings` |
| Don't record this call | Skips the call in progress (discards what was recorded) and stays off until Teams stops using the microphone |
| Launch at login | Starts Edison silently when the Mac starts (on by default; untick to disable) |
| Pause auto-detection | Stops recording new calls |

The icon turns red while recording.

### Manager window

- **Calls**: search, filter, play audio, read summary/transcript (click a line to jump to it), rename, reprocess, delete (moves to Trash). Each call gets an LLM-written title and one-line description.
- **Review**: unidentified speakers; listen to a clip and assign a name. Voices are remembered locally, several voiceprints per person are supported.
- **People**: rename, merge, delete people and voiceprints.

- **Settings**: everything in `config.json` plus the vocabulary, without touching files: minimum call length, meeting-title exclusions, notifications, Whisper and LLM models, audio retention.

### Notifications

When a call has been processed Edison shows a notification (title, one-line description, or how many speakers need identifying). Clicking it opens that call. It is skipped while the Edison window is in front. Turn it off in Settings.

### What is not recorded

- Calls shorter than `min_call_seconds` (default 15 s), e.g. a microphone test in Teams.
- Meetings with a Teams window title containing any word in `exclude_titles`.
- A call you skip with *Don't record this call*.

## Output

```
~/Teams Recordings/YYYY-MM-DD/HHMM_call/
  call.m4a            mixed audio (deleted after the retention period)
  transcript.md       corrected transcript with speaker names
  transcript_raw.md   raw Whisper output
  summary.md          summary, decisions, action items
  transcript.json     structured data used by the window
```

## Configuration

Edit these in the **Settings** tab, or directly:

- `config.json`: `audio_retention_days` (30; null keeps forever), `keep_raw_tracks` (false), `notify_when_done` (true), `min_call_seconds` (15), `exclude_titles` ([]), `whisper_model`, `polish_model`.
- `vocabulary.txt`: names and terms (one per line) to help transcription.
- Logs: `~/Teams Recordings/recorder.log` and `processing.log`.

## Layout

`Sources/main.swift` (app) · `make_cert.sh` (signing identity) · `transcribe.sh` (queue) · `process_call.py` (pipeline) · `server.py` + `ui/` (manager window) · `voices.py` (voice database) · `polish.py`, `summary.py` (local LLM)

## Troubleshooting

- **No recording / permission error:** System Settings → Privacy & Security → enable Edison under Microphone and Screen & System Audio Recording. Builds are signed with a local self-signed identity ("Edison Local Signing", created by `make_cert.sh`), so permissions survive updates. If that identity is missing the app is signed ad-hoc and macOS asks again after each rebuild.
- **Call not processed:** open Edison; unprocessed calls show a banner in the sidebar. Click *Process unprocessed*.
