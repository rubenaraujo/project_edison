"""Generate (or regenerate) title + description for one call with the local LLM. Usage: describe_call.py <call_folder>"""
import os
import sys

import common
import summary
from process_call import glossary


def main(folder):
    pidfile = os.path.join(folder, ".describing")
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        data = common.load(folder)
        if not data.get("summary") and sum(len(u["text"]) for u in data["utterances"]) >= 200:
            data["summary"] = summary.summarize(data, glossary())
        title, desc = summary.describe(data, glossary())
        data["title"], data["description"] = title or "No speech detected", desc or ""
        common.save(folder, data)
    finally:
        os.remove(pidfile)


if __name__ == "__main__":
    main(sys.argv[1])
