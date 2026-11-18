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

It creates the Python environment, downloads the models (Whisper large-v3, Qwen2.5-14B, speaker models), builds `~/Applications/Edison.app`, and starts it. Safe to re-run (also use it to update after changing the code).

On first run macOS asks for **Microphone** and **Screen & System Audio Recording** access. Both are required: the second is how the other participants' audio is captured.

Remove with `./uninstall.sh` (recordings and voice database are kept).

## Using it

Edison detects when Teams starts using the microphone and records automatically. Menu-bar icon:

| Item | What it does |
| --- | --- |
| Open Edison… | Opens the manager window |
| Open recordings folder | Opens `~/Teams Recordings` |
| Launch at login | Starts Edison silently when the Mac starts (on by default; untick to disable) |
| Pause auto-detection | Stops recording new calls |

The icon turns red while recording.

### Manager window

- **Calls**: search, filter, play audio, read summary/transcript (click a line to jump to it), rename, reprocess, delete (moves to Trash). Each call gets an LLM-written title and one-line description.
- **Review**: unidentified speakers; listen to a clip and assign a name. Voices are remembered locally, several voiceprints per person are supported.
- **People**: rename, merge, delete people and voiceprints.

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

- `config.json`: `audio_retention_days` (default 30), `keep_raw_tracks` (default false).
- `vocabulary.txt`: names and terms (one per line) to help transcription.
- Logs: `~/Teams Recordings/recorder.log` and `processing.log`.

## Layout

`Sources/main.swift` (app) · `transcribe.sh` (queue) · `process_call.py` (pipeline) · `server.py` + `ui/` (manager window) · `voices.py` (voice database) · `polish.py`, `summary.py` (local LLM)

## Troubleshooting

- **No recording / permission error:** System Settings → Privacy & Security → enable Edison under Microphone and Screen & System Audio Recording. After a rebuild the signature changes and macOS may ask again.
- **Call not processed:** open Edison; unprocessed calls show a banner in the sidebar. Click *Process unprocessed*.
