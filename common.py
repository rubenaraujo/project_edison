import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("TEAMS_REC_FOLDER") or os.path.expanduser("~/Teams Recordings")
PENDING = ("unknown", "suggested")
CONFIG_PATH = os.path.join(HERE, "config.json")
DEFAULTS = {
    "audio_retention_days": 30,
    "keep_raw_tracks": False,
    "notify_when_done": True,
    "min_call_seconds": 15,
    "exclude_titles": [],
    "whisper_model": "mlx-community/whisper-large-v3-mlx",
    "polish_model": "mlx-community/Qwen2.5-14B-Instruct-4bit",
}


def config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(CONFIG_PATH + ".tmp", CONFIG_PATH)


def calls():
    return sorted(glob.glob(os.path.join(BASE, "*", "*_call")))


def load(folder):
    with open(os.path.join(folder, "transcript.json"), encoding="utf-8") as f:
        return json.load(f)


def save(folder, data):
    path = os.path.join(folder, "transcript.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    with open(os.path.join(folder, "transcript.md"), "w", encoding="utf-8") as f:
        f.write(render_md(data))
    with open(os.path.join(folder, "transcript_raw.md"), "w", encoding="utf-8") as f:
        f.write(render_md(data, raw=True))
    if data.get("summary"):
        with open(os.path.join(folder, "summary.md"), "w", encoding="utf-8") as f:
            f.write(render_summary(data))


def cluster(data, cid):
    return next((c for c in data["clusters"] if c["id"] == cid), None)


def label(data, spk):
    if spk == "me":
        return "Me"
    c = cluster(data, spk)
    if c is None:
        return "Speaker ?"
    return c.get("name") or f"Speaker {c['n']}"


def fmt(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def relabel(data, text):
    """LLM texts refer to speakers as 'Speaker N'; swap in names once they are identified."""
    def sub(m):
        c = next((c for c in data["clusters"] if c["n"] == int(m.group(1))), None)
        return c["name"] if c and c.get("name") else m.group(0)
    return re.sub(r"Speaker (\d+)", sub, text or "")


def render_summary(data):
    return "# Summary\n\n" + relabel(data, data["summary"]) + "\n"


def render_md(data, raw=False):
    lines = ["# Transcript (raw, as heard by Whisper)\n" if raw else "# Transcript\n"]
    for u in data["utterances"]:
        text = u.get("text_raw", u["text"]) if raw else u["text"]
        lines.append(f"**[{fmt(u['start'])}] {label(data, u['speaker'])}:** {text}\n")
    return "\n".join(lines)
