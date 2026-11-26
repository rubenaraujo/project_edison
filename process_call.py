"""Process one call: transcribe (EN/PT per chunk) -> diarize 'others' -> match voices -> polish -> save.

Usage: process_call.py <call_folder> <tmp_dir_with_me.wav_and_others.wav>
"""
import difflib
import os
import re
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


PROMPTS = {
    "pt": "Reunião de trabalho em português de Portugal, com termos técnicos em inglês (deploy, bucket, policy, "
          "pipeline, backlog, sprint, wildcard). ",
    "en": "Work meeting in English. ",
}
# Short fillers Whisper invents on silence/noise
HALLUCINATIONS = {"okay", "ok", "thank you", "thanks", "thanks for watching", "thank you for watching",
                  "thanks man", "obrigado", "obrigada", "you", "bye", "see you", "me too", "uh huh",
                  "youre welcome", "all rights reserved", "legendas pela comunidade amara.org"}


LOOP = re.compile(r"(.{3,60}?)(?:\s*\1){3,}", re.I | re.S)


def junk_segment(seg):
    """True for segments Whisper made up from noise, overlapping voices or silence."""
    text = seg["text"].strip()
    lp, ns = seg.get("avg_logprob", 0), seg.get("no_speech_prob", 0)
    ws = seg.get("words", [])
    mp = sum(w.get("probability", 1) for w in ws) / len(ws) if ws else 1
    plain = re.sub(r"[^\w ]+", "", text.lower()).strip()
    if lp < -1.0 or (lp < -0.9 and mp < 0.3):
        return True
    if LOOP.search(text):  # stuck repeating the same phrase
        return True
    if re.search(r"[^\u0000-\u024F\u2000-\u206F€£]", text) or re.search(r"(.)\1{3,}", text):
        return True  # other scripts, symbols, stuck characters
    if plain in HALLUCINATIONS and (ns > 0.3 or mp < 0.6 or lp < -0.4):
        return True
    return len(plain.split()) <= 4 and ns > 0.3 and mp < 0.6


def detect_lang(chunk, model, last_lang):
    """Choose between PT and EN only (Whisper otherwise drifts to Korean, Spanish, ...)."""
    import mlx.core as mx
    from mlx_whisper.audio import N_FRAMES, log_mel_spectrogram, pad_or_trim

    mel = log_mel_spectrogram(chunk, n_mels=model.dims.n_mels)
    _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
    pt, en = float(probs.get("pt", 0)), float(probs.get("en", 0))
    if abs(pt - en) < 0.15:  # unsure: Portuguese speakers mostly stay in their language
        return last_lang
    return "pt" if pt > en else "en"


def _rx(pattern, rep):
    return re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)", re.I), rep


# Heard wrongly in any language
FIXES = [
    _rx(r"yield attack|yodatact|data acton|dtac|datar|dataque|detacto|dat[ae]ct", "Data Act"),
    _rx(r"computador pc", "VPC"),
    _rx(r"alamir", "Miriam"),
    _rx(r"cris", "Chris"),
    _rx(r"(?:subtitles by the amara\.org community|legendas pela comunidade amara\.org)\.?", ""),
]
# Spoken as the Portuguese/English word but heard differently (applied to Portuguese speech only)
PT_FIXES = [
    (re.compile(r"(?<!\w)(?:epic|ipic)(s?)(?!\w)", re.I), lambda m: "épico" + m.group(1)),
    (re.compile(r"(?<!\w)(rog|roll)(s?)(?!\w)", re.I),
     lambda m: ("Role" if m.group(1)[0].isupper() else "role") + m.group(2)),
    _rx(r"taques", "tags"),
    _rx(r"polistas", "policies"),
    _rx(r"setaca|stag", "stack"),
    _rx(r"cheque de ploys destaques", "check de deploys das stacks"),
]


def fix_text(text, lang):
    for rx, rep in FIXES + (PT_FIXES if lang == "pt" else []):
        text = rx.sub(rep, text)
    return re.sub(r"\s{2,}", " ", text).strip()


def collapse_repeats(words):
    """Whisper loops ('E aí E aí E aí ...'): keep one copy of any 1-4 word phrase repeated back to back."""
    norm = lambda w: re.sub(r"\W+", "", w["w"].lower())
    keys = [norm(w) for w in words]
    out, i = [], 0
    while i < len(words):
        for n in (1, 2, 3, 4):
            need = 4 if n == 1 else 3
            reps = 1
            while keys[i : i + n] == keys[i + reps * n : i + (reps + 1) * n] and len(keys[i : i + n]) == n:
                reps += 1
            if reps >= need and all(keys[i : i + n]):
                if reps < 6:
                    out.extend(words[i : i + n])
                i += reps * n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def transcribe_words(audio, prompt):
    """Per-chunk language (PT/EN only), word timestamps. -> list of word dicts."""
    import mlx_whisper
    from mlx_whisper.transcribe import ModelHolder
    import mlx.core as mx

    model = ModelHolder.get_model(WHISPER, mx.float16)
    words, last_lang = [], "pt"
    for s, e in chunk_bounds(audio):
        chunk = audio[s:e]
        if len(chunk) < SR // 2 or float(np.sqrt((chunk**2).mean())) < 0.002:
            continue
        lang = detect_lang(chunk, model, last_lang)
        last_lang = lang
        res = mlx_whisper.transcribe(
            chunk, path_or_hf_repo=WHISPER, word_timestamps=True, condition_on_previous_text=False,
            language=lang, initial_prompt=PROMPTS[lang] + prompt, hallucination_silence_threshold=1.5,
            compression_ratio_threshold=2.0, logprob_threshold=-0.8, no_speech_threshold=0.5)
        for seg in res["segments"]:
            if seg.get("no_speech_prob", 0) > 0.6 and seg.get("avg_logprob", 0) < -1.0:
                continue
            if junk_segment(seg):
                continue
            toks = [t for t in re.sub(r"[^\w ]+", "", seg["text"].lower()).split()]
            if len(toks) >= 8 and len(set(toks)) / len(toks) < 0.3:  # repetition loop
                continue
            for w in seg.get("words", []):
                words.append({"start": s / SR + w["start"], "end": s / SR + w["end"],
                              "w": w["word"], "lang": lang})
    return collapse_repeats(words)


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
        # same speech transcribed slightly differently on the mic track: compare as text
        theirs = " ".join(norm(w["w"]) for w in other_words if buf[0]["start"] - 1 <= w["start"] <= buf[-1]["end"] + 1)
        if len(mine) >= 4 and difflib.SequenceMatcher(None, " ".join(mine), theirs).ratio() >= 0.6:
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
            gl = polish.auto_glossary([u["text_raw"] for u in utts], gl, log)
            for u, t in zip(utts, polish.polish([u["text_raw"] for u in utts], gl, log)):
                u["text"] = t
        except Exception as ex:
            log("polish failed, keeping raw text:", ex)

    for u in utts:
        u["text"] = fix_text(u["text"], u.get("lang"))
    utts = [u for u in utts if u["text"]]

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
