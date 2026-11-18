"""Daily housekeeping: delete old call audio (keeps transcripts, summaries and pending-review clips).

Retention is set in config.json ("audio_retention_days"; 0 or null = keep forever).
"""
import datetime
import glob
import json
import os
import time

import common

CONFIG = os.path.join(common.HERE, "config.json")


def retention_days():
    try:
        with open(CONFIG) as f:
            return json.load(f).get("audio_retention_days")
    except Exception:
        return 30


def main():
    days = retention_days()
    if not days:
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=int(days))
    for folder in common.calls():
        try:
            day = datetime.date.fromisoformat(os.path.basename(os.path.dirname(folder)))
        except ValueError:
            continue
        if day > cutoff or not os.path.exists(os.path.join(folder, "transcript.json")):
            continue  # too recent, or not processed yet
        data = common.load(folder)
        pending = {c["id"] for c in data["clusters"] if c["status"] in common.PENDING}
        raw = [f for n in ("me", "others") for e in ("caf", "m4a") for f in glob.glob(os.path.join(folder, f"{n}.{e}"))]
        for f in raw + glob.glob(os.path.join(folder, "call.m4a")) + glob.glob(os.path.join(folder, "speakers", "*.wav")):
            base = os.path.basename(f)
            if base.startswith("c") and base.split("_")[0][1:].isdigit() and int(base.split("_")[0][1:]) in pending:
                continue  # still needed for speaker review
            os.remove(f)
            print(time.strftime("%F %T"), "deleted", f, flush=True)


if __name__ == "__main__":
    main()
