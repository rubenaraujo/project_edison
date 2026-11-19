"""Process one call: transcribe (EN/PT per chunk) -> diarize 'others' -> match voices -> polish -> save.

Usage: process_call.py <call_folder> <tmp_dir_with_me.wav_and_others.wav>
"""
import os
import subprocess
import sys
import time
import wave

import numpy as np

import common
import voices

HERE = common.HERE
WHISPER = os.environ.get("WHISPER_MODEL") or common.config()["whisper_model"]
SEG_MODEL = os.path.join(HERE, "models", "sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
EMB_MODEL = os.path.join(HERE, "models", "wespeaker_en_voxceleb_resnet293_LM.onnx")
VOCAB = os.path.join(HERE, "vocabulary.txt")
SR = 16000
MINOR_SECONDS = 4.0  # clusters with less speech than this are never queued for review


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def load_wav(path):
    if not os.path.exists(path):
        return None
    with wave.open(path) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def glossary():
    if not os.path.exists(VOCAB):
        return []
    with open(VOCAB, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------- ASR

def chunk_bounds(a, min_len=6, max_len=30, min_gap=0.5):
    """Cut at silences (>= min_gap s) into 6-30 s chunks: short enough that language switches
    (EN <-> PT) fall on chunk boundaries, long enough for reliable language detection."""
    frame = SR // 20
    k = len(a) // frame
    e = np.sqrt((a[: k * frame].reshape(k, frame) ** 2).mean(1)) if k else np.zeros(0)
    quiet = e < max(0.004, 0.08 * float(np.percentile(e, 90))) if k else np.zeros(0, bool)
    cuts, i = [], 0
    while i < k:  # centres of silent runs
        if quiet[i]:
            j = i
            while j < k and quiet[j]:
                j += 1
            if (j - i) * frame >= min_gap * SR:
                cuts.append(((i + j) // 2) * frame)
            i = j
        else:
            i += 1
    bounds, pos = [0], 0
    while True:
        ok = [c for c in cuts if pos + min_len * SR <= c <= min(pos + max_len * SR, len(a) - SR)]
        if ok:
            pos = ok[0]
        elif len(a) - pos > max_len * SR:
            pos += max_len * SR
        else:
            break
        bounds.append(pos)
    bounds.append(len(a))
    return list(zip(bounds[:-1], bounds[1:]))


def transcribe_words(audio, prompt):
    """Per-chunk language detection (EN/PT), word timestamps. -> list of word dicts."""
    import mlx_whisper

    words, last_lang = [], "pt"
    for s, e in chunk_bounds(audio):
        chunk = audio[s:e]
        if len(chunk) < SR // 2 or float(np.sqrt((chunk**2).mean())) < 0.002:
            continue
        kw = dict(path_or_hf_repo=WHISPER, word_timestamps=True, condition_on_previous_text=False,
                  initial_prompt=prompt or None)
        res = mlx_whisper.transcribe(chunk, **kw)
        if res.get("language") not in ("en", "pt"):
            res = mlx_whisper.transcribe(chunk, language=last_lang, **kw)
        lang = res.get("language", last_lang)
        last_lang = lang
        for seg in res["segments"]:
            if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -1.0:
                continue
            for w in seg.get("words", []):
                words.append({"start": s / SR + w["start"], "end": s / SR + w["end"],
                              "w": w["word"], "lang": lang})
    return words


# ---------------------------------------------------------------- diarization

def diarize(audio):
    import sherpa_onnx as so

    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL), num_threads=4),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=4),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=0.5),
        min_duration_on=0.3, min_duration_off=0.5)
    sd = so.OfflineSpeakerDiarization(cfg)
    res = sd.process(audio).sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in res]


def embed(audio, segs, cap=45.0):
    import sherpa_onnx as so

    ext = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=4))
    parts, total = [], 0.0
    for s, e in sorted(segs, key=lambda x: x[0] - x[1]):
        parts.append(audio[int(s * SR) : int(e * SR)])
        total += e - s
        if total >= cap:
            break
    st = ext.create_stream()
    st.accept_waveform(sample_rate=SR, waveform=np.concatenate(parts))
    st.input_finished()
    return np.array(ext.compute(st), dtype=np.float32)


def write_clip(path, audio, s, e):
    pcm = (np.clip(audio[int(s * SR) : int(e * SR)], -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def build_clusters(others, words, folder):
    segs = diarize(others)
    if not segs:  # no speech detected: anything Whisper produced is silence hallucination
        return [], []
    order = []
    for _, _, spk in segs:
        if spk not in order:
            order.append(spk)
    ids = {spk: i for i, spk in enumerate(order)}
    by = {i: [] for i in ids.values()}
    for s, e, spk in segs:
        by[ids[spk]].append((s, e))

    # words -> speaker by midpoint, falling back to nearest segment (<1 s) then previous word
    starts = np.array([s for s, _, _ in segs]); ends = np.array([e for _, e, _ in segs])
    prev = None
    for w in words:
        m = (w["start"] + w["end"]) / 2
        hit = np.where((starts <= m) & (m <= ends))[0]
        if len(hit):
            idx = hit[0]
        else:
            d = np.minimum(abs(starts - m), abs(ends - m)); idx = int(d.argmin())
            if d[idx] > 1.0:
                idx = None
        w["spk"] = ids[segs[idx][2]] if idx is not None else prev
        prev = w["spk"] if w["spk"] is not None else prev
    for w in words:
        if w["spk"] is None:
            w["spk"] = 0

    os.makedirs(os.path.join(folder, "speakers"), exist_ok=True)
    clusters = []
    for cid, cs in by.items():
        dur = sum(e - s for s, e in cs)
        emb = embed(others, cs)
        status, pid, name, score, sugg = voices.classify(emb)
        if status == "unknown" and dur < MINOR_SECONDS:
            status = "minor"
        if status == "suggested" and dur < MINOR_SECONDS:
            status = "minor"
        clips = []
        for k, (s, e) in enumerate(sorted(cs, key=lambda x: x[0] - x[1])[:3]):
            if e - s < 1.5:
                continue
            e = min(e, s + 8)
            fn = f"speakers/c{cid}_{k}.wav"
            write_clip(os.path.join(folder, fn), others, s, e)
            text = "".join(w["w"] for w in words if w["spk"] == cid and s <= w["start"] <= e).strip()
            clips.append({"file": fn, "start": round(s, 1), "end": round(e, 1), "text": text})
        clusters.append({"id": cid, "n": cid + 1, "duration": round(dur, 1), "embedding": emb.tolist(),
                         "status": status, "person_id": pid, "name": name, "score": round(score, 3),
                         "suggestions": sugg, "clips": clips})
    return clusters, words


# ---------------------------------------------------------------- assemble

def utterances(words):
    words = sorted(words, key=lambda w: w["start"])
    out = []
    for w in words:
        u = out[-1] if out else None
        if u and u["speaker"] == w["spk"] and w["start"] - u["end"] <= 2.0:
            u["text"] += w["w"]; u["end"] = w["end"]
        else:
            out.append({"start": round(w["start"], 2), "end": w["end"], "speaker": w["spk"],
                        "text": w["w"], "lang": w["lang"]})
    for u in out:
        u["text"] = u["text"].strip(); u["end"] = round(u["end"], 2)
    return [u for u in out if u["text"]]


def drop_echo(me_words, other_words):
    """If the mic picked up the other side through the speakers, drop that echo from 'Me'."""
    import re
    norm = lambda t: re.sub(r"\W+", "", t.lower())
    other_words = sorted(other_words, key=lambda w: w["start"])
    out, buf = [], []

    def flush():
        if not buf:
            return
        win = {norm(w["w"]) for w in other_words if buf[0]["start"] - 1 <= w["start"] <= buf[-1]["end"] + 1}
        mine = [norm(w["w"]) for w in buf if norm(w["w"])]
        if len(mine) >= 3 and sum(x in win for x in mine) / len(mine) >= 0.7:
            return
        out.extend(buf)

    for w in sorted(me_words, key=lambda w: w["start"]):
        if buf and w["start"] - buf[-1]["end"] > 2.0:
            flush(); buf = []
        buf.append(w)
    flush()
    return out


def main(folder, tmp):
    me = load_wav(os.path.join(tmp, "me.wav"))
    others = load_wav(os.path.join(tmp, "others.wav"))
    gl = glossary()
    prompt = ("Glossary: " + ", ".join(gl) + ".") if gl else ""

    others_words, clusters = [], []
    if others is not None:
        log("transcribing others"); others_words = transcribe_words(others, prompt)
        if others_words:
            log("diarizing"); clusters, others_words = build_clusters(others, others_words, folder)
    me_words = []
    if me is not None:
        log("transcribing me"); me_words = transcribe_words(me, prompt)
        for w in me_words:
            w["spk"] = "me"
        me_words = drop_echo(me_words, others_words)

    utts = sorted(utterances(me_words) + utterances(others_words), key=lambda u: u["start"])
    for u in utts:
        u["text_raw"] = u["text"]
    if utts and not os.environ.get("SKIP_POLISH"):
        log("polishing")
        try:
            import polish
            for u, t in zip(utts, polish.polish([u["text_raw"] for u in utts], gl, log)):
                u["text"] = t
        except Exception as ex:
            log("polish failed, keeping raw text:", ex)

    seconds = max((len(x) for x in (me, others) if x is not None), default=0) / SR
    data = {"version": 1, "duration": round(seconds, 1), "utterances": utts, "clusters": clusters}
    common.save(folder, data)
    if utts and not os.environ.get("SKIP_POLISH"):
        try:
            import summary
            data["summary"] = summary.summarize(data, gl, log)
            data["title"], data["description"] = summary.describe(data, gl, log)
            common.save(folder, data)
        except Exception as ex:
            log("summary failed:", ex)
    if not utts:
        data["title"], data["description"] = "No speech detected", ""
        common.save(folder, data)
    pending = sum(c["status"] in common.PENDING for c in clusters)
    log(f"saved transcript ({len(utts)} utterances, {len(clusters)} speakers, {pending} to review)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
