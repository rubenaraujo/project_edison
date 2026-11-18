"""Edison manager backend: local API + UI served on 127.0.0.1 only (shown in the app's window)."""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import common
import voices

PORT = int(os.environ.get("EDISON_PORT", "8765"))
TOKEN = secrets.token_urlsafe(24)
UI = os.path.join(common.HERE, "ui", "index.html")
BASE_REAL = os.path.realpath(common.BASE)
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/\d{4}_call$")
TRACKS = ("me.caf", "others.caf", "me.m4a", "others.m4a")
ENV = dict(os.environ, TEAMS_REC_FOLDER=common.BASE)
_cache = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ helpers

class Err(Exception):
    pass


def call_dir(cid):
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise Err("bad call id")
    d = os.path.join(common.BASE, cid)
    if not os.path.isdir(d):
        raise Err("call not found")
    return d


def alive(pidfile):
    try:
        os.kill(int(open(pidfile).read().strip()), 0)
        return True
    except Exception:
        return False


def status_of(d):
    if alive(os.path.join(d, ".processing")) or alive(os.path.join(d, ".describing")):
        return "processing"
    if os.path.exists(os.path.join(d, "transcript.json")):
        return "ready"
    if any(os.path.exists(os.path.join(d, t)) for t in TRACKS):
        try:
            tries = int(open(os.path.join(d, ".attempts")).read().strip())
        except Exception:
            tries = 0
        return "failed" if tries >= 3 else "pending"
    return "empty"


def call_ids():
    out = []
    for d in common.calls():
        out.append(os.path.relpath(d, common.BASE))
    return sorted(out, reverse=True)


def audio_duration(d):
    f = os.path.join(d, "call.m4a")
    if not os.path.exists(f):
        return None
    try:
        out = subprocess.run(["afinfo", f], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"estimated duration:\s*([\d.]+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def loaded(cid):
    """(data, blob) cached by transcript mtime."""
    d = call_dir(cid)
    path = os.path.join(d, "transcript.json")
    if not os.path.exists(path):
        return None, ""
    mt = os.path.getmtime(path)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mt:
            return hit[1], hit[2]
    data = common.load(d)
    names = " ".join(common.label(data, c["id"]) for c in data["clusters"])
    blob = " ".join([common.relabel(data, data.get("title") or ""), common.relabel(data, data.get("description") or ""),
                     common.relabel(data, data.get("summary") or ""), names,
                     " ".join(u["text"] for u in data["utterances"])]).lower()
    with _lock:
        _cache[path] = (mt, data, blob)
    return data, blob


def row(cid):
    d = call_dir(cid)
    day, name = cid.split("/")
    hhmm = name[:4]
    r = {"id": cid, "day": day, "time": f"{hhmm[:2]}:{hhmm[2:]}", "status": status_of(d),
         "has_audio": os.path.exists(os.path.join(d, "call.m4a")), "title": None, "description": "",
         "duration": None, "speakers": [], "pending": 0, "described": False}
    data, _ = loaded(cid)
    if data:
        r["title"] = common.relabel(data, data.get("title") or "") or None
        r["description"] = common.relabel(data, data.get("description") or "")
        r["described"] = bool(data.get("title"))
        r["duration"] = data.get("duration") or audio_duration(d)
        sp = []
        if any(u["speaker"] == "me" for u in data["utterances"]):
            sp.append("Me")
        used = {u["speaker"] for u in data["utterances"]}
        for c in data["clusters"]:
            if c["id"] in used and common.label(data, c["id"]) not in sp:
                sp.append(common.label(data, c["id"]))
        r["speakers"] = sp
        r["pending"] = sum(c["status"] in common.PENDING for c in data["clusters"])
    else:
        r["duration"] = audio_duration(d)
    return r


def snippet(cid, terms):
    data, _ = loaded(cid)
    if not data:
        return None
    for u in data["utterances"]:
        t = u["text"].lower()
        if all(x in t for x in terms[:1]):
            return {"t": u["start"], "text": u["text"], "who": common.label(data, u["speaker"])}
    return None


def list_calls(q="", flt="all"):
    terms = [t for t in re.split(r"\s+", q.lower().strip()) if t]
    out = []
    for cid in call_ids():
        r = row(cid)
        if flt == "review" and not r["pending"]:
            continue
        if flt == "attention" and r["status"] not in ("pending", "failed", "processing"):
            continue
        if terms:
            _, blob = loaded(cid)
            if not all(t in blob for t in terms):
                continue
            r["snippet"] = snippet(cid, terms)
        out.append(r)
    return out


def detail(cid):
    r = row(cid)
    data, _ = loaded(cid)
    if data:
        r["utterances"] = [{"start": u["start"], "end": u["end"], "who": common.label(data, u["speaker"]),
                            "me": u["speaker"] == "me", "cluster": None if u["speaker"] == "me" else u["speaker"],
                            "text": u["text"], "raw": u.get("text_raw", u["text"])} for u in data["utterances"]]
        r["clusters"] = [{"id": c["id"], "n": c["n"], "label": common.label(data, c["id"]), "name": c.get("name"),
                          "person_id": c.get("person_id"), "status": c["status"], "duration": c["duration"],
                          "suggestions": c.get("suggestions", []), "clips": c["clips"]} for c in data["clusters"]]
        r["summary"] = common.relabel(data, data.get("summary") or "")
    return r


def spawn(args):
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     start_new_session=True, env=ENV, cwd=common.HERE)


def global_status():
    running = os.path.exists(os.path.join(common.BASE, ".queue.lock"))
    active = review = pending = failed = 0
    for cid in call_ids():
        d = os.path.join(common.BASE, cid)
        st = status_of(d)
        active += st == "processing"
        pending += st == "pending"
        failed += st == "failed"
        if st == "ready":
            data, _ = loaded(cid)
            review += sum(c["status"] in common.PENDING for c in data["clusters"])
    return {"processing": min(active, 1) if running else active, "waiting": max(active - 1, 0) if running else 0,
            "review": review, "pending": pending, "failed": failed}


# ------------------------------------------------------------------ actions

def trash(cid):
    d = call_dir(cid)
    if status_of(d) == "processing":
        raise Err("call is being processed")
    dest_dir = os.path.expanduser("~/.Trash")
    base = "Edison " + cid.replace("/", " ")
    dest, n = os.path.join(dest_dir, base), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(dest_dir, f"{base} {n}")
    shutil.move(d, dest)
    day = os.path.dirname(d)
    if not os.listdir(day) or os.listdir(day) == [".DS_Store"]:
        shutil.rmtree(day, ignore_errors=True)


def reprocess(cid):
    d = call_dir(cid)
    if status_of(d) == "processing":
        return
    if not os.path.exists(os.path.join(d, "call.m4a")) and not any(os.path.exists(os.path.join(d, t)) for t in TRACKS):
        raise Err("no audio left for this call")
    spawn(["/bin/bash", os.path.join(common.HERE, "transcribe.sh"), d, "--reprocess"])


def process_pending():
    n = 0
    for cid in call_ids():
        d = os.path.join(common.BASE, cid)
        if status_of(d) in ("pending", "failed"):
            try:
                os.remove(os.path.join(d, ".attempts"))
            except OSError:
                pass
            spawn(["/bin/bash", os.path.join(common.HERE, "transcribe.sh"), d])
            n += 1
    return n


def describe(cid):
    d = call_dir(cid)
    if status_of(d) == "processing":
        return
    spawn([os.path.join(common.HERE, "venv", "bin", "python"), os.path.join(common.HERE, "describe_call.py"), d])


def update_call(cid, title):
    d = call_dir(cid)
    data = common.load(d)
    data["title"] = title.strip()
    common.save(d, data)


def refresh_names(remap=None):
    """Keep call transcripts consistent after people were renamed, merged or deleted."""
    for d in common.calls():
        try:
            data = common.load(d)
        except Exception:
            continue
        changed = False
        for c in data["clusters"]:
            pid = c.get("person_id")
            if not pid:
                continue
            if remap and pid in remap:
                pid = c["person_id"] = remap[pid]
            nm = voices.person_name(pid)
            if nm is None:
                c.update(person_id=None, name=None, status="unknown", suggestions=[])
                changed = True
            elif nm != c.get("name"):
                c["name"] = nm
                changed = True
        if changed:
            common.save(d, data)


def rematch_all():
    for d in common.calls():
        try:
            data = common.load(d)
        except Exception:
            continue
        changed = False
        for c in data["clusters"]:
            if c["status"] in common.PENDING:
                st, pid, name, score, sugg = voices.classify(c["embedding"])
                if st == "matched":
                    c.update(status="matched", person_id=pid, name=name, score=score, suggestions=sugg)
                    changed = True
                else:
                    c.update(suggestions=sugg, status="suggested" if sugg else "unknown")
        if changed:
            common.save(d, data)


def assign(b):
    d = call_dir(b["call"])
    data = common.load(d)
    c = common.cluster(data, b["cluster"])
    if c is None:
        raise Err("unknown speaker")
    if b.get("unassign"):
        st, pid, name, score, sugg = voices.classify(c["embedding"])
        c.update(status="suggested" if sugg else "unknown", person_id=None, name=None, suggestions=sugg)
    elif b.get("ignore"):
        c.update(status="ignored", person_id=None, name=None)
    else:
        if b.get("person_id"):
            pid = int(b["person_id"])
        elif (b.get("name") or "").strip():
            pid = voices.person_id(b["name"])
        else:
            raise Err("pick a person or type a name")
        if b.get("add_print", True):
            voices.add_print(pid, c["embedding"], b.get("device", ""), b["call"])
        c.update(status="assigned", person_id=pid, name=voices.person_name(pid), suggestions=[])
    common.save(d, data)
    rematch_all()


def people():
    review = {}
    for cid in call_ids():
        data, _ = loaded(cid)
        for c in (data or {"clusters": []})["clusters"]:
            if c.get("person_id"):
                review.setdefault(c["person_id"], set()).add(cid)
    return [{"id": p["id"], "name": p["name"], "prints": voices.prints(p["id"]), "calls": len(review.get(p["id"], []))}
            for p in voices.people()]


def review_items():
    items = []
    for cid in call_ids():
        data, _ = loaded(cid)
        if not data:
            continue
        for c in data["clusters"]:
            if c["status"] in common.PENDING:
                items.append({"call": cid, "title": common.relabel(data, data.get("title") or ""), "cluster": c["id"],
                              "n": c["n"], "duration": c["duration"], "suggestions": c.get("suggestions", []),
                              "clips": c["clips"]})
    return items


def people_action(b):
    a = b["action"]
    if a == "rename":
        if not b["name"].strip():
            raise Err("empty name")
        voices.rename_person(int(b["id"]), b["name"])
    elif a == "merge":
        src, dst = int(b["id"]), int(b["into"])
        if src == dst:
            raise Err("pick a different person")
        voices.merge_people(src, dst)
        refresh_names({src: dst})
        return
    elif a == "delete":
        voices.delete_person(int(b["id"]))
    elif a == "delete_print":
        voices.delete_print(int(b["id"]))
    elif a == "update_print":
        voices.update_print(int(b["id"]), b.get("device", ""))
    else:
        raise Err("unknown action")
    refresh_names()


# ------------------------------------------------------------------ http

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def file(self, path, ctype):
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(size - int(m.group(2)), 0)
            end = min(end, size - 1)
            code = 206
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(end - start + 1)
        extra = {"Accept-Ranges": "bytes"}
        if code == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self.send(code, data, ctype, extra)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, "forbidden", "text/plain")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                with open(UI, encoding="utf-8") as f:
                    return self.send(200, f.read().replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
            if u.path == "/logo.png":
                return self.file(os.path.join(common.HERE, "assets", "logo.png"), "image/png")
            if u.path == "/api/calls":
                return self.send(200, json.dumps({"calls": list_calls(q.get("q", ""), q.get("filter", "all")),
                                                  "status": global_status()}))
            if u.path == "/api/call":
                return self.send(200, json.dumps(detail(q["id"])))
            if u.path == "/api/people":
                return self.send(200, json.dumps({"people": people()}))
            if u.path == "/api/review":
                return self.send(200, json.dumps({"items": review_items(), "people": voices.people()}))
            if u.path == "/api/status":
                return self.send(200, json.dumps(global_status()))
            if u.path == "/audio":
                p = os.path.join(call_dir(q["call"]), "call.m4a")
                if not os.path.exists(p):
                    return self.send(404, "no audio", "text/plain")
                return self.file(p, "audio/mp4")
            if u.path == "/clip":
                p = os.path.realpath(os.path.join(call_dir(q["call"]), q["file"]))
                if not p.startswith(BASE_REAL + os.sep) or not os.path.isfile(p):
                    return self.send(404, "not found", "text/plain")
                return self.file(p, "audio/wav")
            self.send(404, "not found", "text/plain")
        except Err as e:
            self.send(400, json.dumps({"error": str(e)}))
        except KeyError:
            self.send(400, json.dumps({"error": "missing parameter"}))
        except Exception as e:  # keep the UI alive
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))

    do_HEAD = do_GET

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Edison-Token") != TOKEN:
            return self.send(403, json.dumps({"error": "forbidden"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or b"{}")
            p = self.path
            if p == "/api/assign":
                assign(b)
            elif p == "/api/calls/delete":
                for cid in b["ids"]:
                    trash(cid)
            elif p == "/api/calls/reprocess":
                for cid in b["ids"]:
                    reprocess(cid)
            elif p == "/api/calls/process-pending":
                return self.send(200, json.dumps({"started": process_pending()}))
            elif p == "/api/calls/describe":
                for cid in b["ids"]:
                    describe(cid)
            elif p == "/api/calls/update":
                update_call(b["id"], b["title"])
            elif p == "/api/calls/reveal":
                subprocess.run(["open", "-R", call_dir(b["id"])], check=False)
            elif p == "/api/people":
                people_action(b)
            elif p == "/api/open-folder":
                subprocess.run(["open", common.BASE], check=False)
            else:
                return self.send(404, json.dumps({"error": "not found"}))
            self.send(200, "{}")
        except Err as e:
            self.send(400, json.dumps({"error": str(e)}))
        except KeyError as e:
            self.send(400, json.dumps({"error": f"missing {e}"}))
        except Exception as e:
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        print("port in use; another Edison server is running")
        return
    srv.daemon_threads = True
    parent = os.getppid()

    def watch():  # exit with the app so no orphan server keeps the port
        while parent > 1 and os.getppid() == parent:
            time.sleep(2)
        os._exit(0)
    if os.environ.get("EDISON_WATCH_PARENT"):
        threading.Thread(target=watch, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    os.chdir(common.HERE)
    sys.exit(main())
