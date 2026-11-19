"""Conservative LLM clean-up of ASR text (local, free). Falls back to the original on any doubt."""
import difflib
import os
import re

import common

MODEL = os.environ.get("POLISH_MODEL") or common.config()["polish_model"]
BATCH = 20

SYSTEM = (
    "You correct speech-recognition errors in transcripts of calls held in English and European "
    "Portuguese. Fix ONLY clear spelling errors, glossary names/terms and punctuation/capitalisation. "
    "Be strictly faithful to what was said: never replace a word with a different word, never "
    "censor, soften, rephrase, translate, summarise, add or remove anything — keep profanity, crude "
    "or sensitive words exactly as transcribed. Keep every line in its original language and "
    "dialect (do not swap Brazilian and European Portuguese words). If a line looks fine, return it "
    "unchanged. Output exactly one line per input line, in the same numbered format 'N. text', "
    "and nothing else."
)

WORD = re.compile(r"\w+", re.UNICODE)


def faithful(old, new):
    """Accept an edit only if every changed word is a near-identical spelling fix
    (no words added, removed or swapped for different ones)."""
    a = [w.lower() for w in WORD.findall(old)]
    b = [w.lower() for w in WORD.findall(new)]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        x, y = "".join(a[i1:i2]), "".join(b[j1:j2])
        if not x or not y:
            return False
        if difflib.SequenceMatcher(None, x, y).ratio() < 0.75:
            return False
    return True


_cache = {}


def get_model():
    if "m" not in _cache:
        from mlx_lm import load

        _cache["m"] = load(MODEL)
    return _cache["m"]


def polish(texts, glossary=(), log=print):
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    model, tok = get_model()
    out = list(texts)
    gloss = ("Glossary: " + ", ".join(glossary) + "\n") if glossary else ""
    for i in range(0, len(texts), BATCH):
        batch = texts[i : i + BATCH]
        ctx = out[max(0, i - 3) : i]
        user = gloss
        if ctx:
            user += "Previous lines (context only, do not output):\n" + "\n".join(ctx) + "\n\n"
        user += "Lines to correct:\n" + "\n".join(f"{n}. {t}" for n, t in enumerate(batch, 1))
        prompt = tok.apply_chat_template(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            add_generation_prompt=True,
            tokenize=False,
        )
        resp = generate(
            model, tok, prompt=prompt, max_tokens=sum(len(t) for t in batch) + 300,
            sampler=make_sampler(temp=0.0),
        )
        parsed = {int(n): t.strip() for n, t in re.findall(r"^(\d+)\.\s*(.+)$", resp, re.M)}
        changed = 0
        for n, orig in enumerate(batch, 1):
            new = parsed.get(n)
            if not new or new == orig:
                continue
            if faithful(orig, new):
                out[i + n - 1] = new
                changed += 1
        log(f"polish batch {i // BATCH + 1}: {changed}/{len(batch)} lines corrected")
    return out
