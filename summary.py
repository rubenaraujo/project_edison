"""Call summary (decisions, action items) with the local LLM."""
import collections

import common
import polish

CHUNK_CHARS = 24000

SYSTEM = (
    "You write concise, faithful minutes of business calls from a transcript. Use ONLY what is in "
    "the transcript; never invent facts, owners, dates or numbers. Speakers appear as 'Me' (the person "
    "who recorded the call) and as 'Speaker N' or by name — refer to them exactly as labelled. "
    "The transcript may contain speech-recognition errors; ignore obvious garbage."
)

FINAL = """Write the minutes in {lang}, in Markdown, with exactly these sections:

## Summary
(3-6 sentences)

## Decisions
(bullet list, or "None")

## Action items
(bullet list "- [ ] Owner — task (deadline if stated)"; if the owner is unclear write "Unassigned"; or "None")

## Open questions
(bullet list, or "None")"""


def _gen(model, tok, user, max_tokens=1200):
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    prompt = tok.apply_chat_template(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        add_generation_prompt=True, tokenize=False)
    return generate(model, tok, prompt=prompt, max_tokens=max_tokens, sampler=make_sampler(temp=0.0)).strip()


def language(data):
    c = collections.Counter()
    for u in data["utterances"]:
        c[u.get("lang", "en")] += len(u["text"])
    return "European Portuguese (pt-PT)" if c.get("pt", 0) > c.get("en", 0) else "English"


def summarize(data, glossary=(), log=print):
    lines = [f"{common.label(data, u['speaker'])}: {u['text']}" for u in data["utterances"]]
    if sum(len(l) for l in lines) < 200:
        return None
    model, tok = polish.get_model()
    gloss = ("Glossary of names and terms: " + ", ".join(glossary) + "\n\n") if glossary else ""

    chunks, cur, size = [], [], 0
    for l in lines:
        if size + len(l) > CHUNK_CHARS and cur:
            chunks.append("\n".join(cur)); cur, size = [], 0
        cur.append(l); size += len(l)
    chunks.append("\n".join(cur))

    if len(chunks) == 1:
        body = chunks[0]
    else:
        notes = []
        for i, ch in enumerate(chunks, 1):
            log(f"summary notes {i}/{len(chunks)}")
            notes.append(_gen(model, tok, gloss + "Extract factual notes (topics, decisions, action items with "
                              "owners, open questions) from this part of a call transcript, as bullets:\n\n" + ch, 900))
        body = "Notes from consecutive parts of the call:\n\n" + "\n\n".join(notes)

    log("writing summary")
    return _gen(model, tok, gloss + FINAL.format(lang=language(data)) + "\n\nTranscript:\n\n" + body, 1200)


DESCRIBE = """Give this call a short title and a one-sentence description, in {lang}.
Reply with exactly two lines and nothing else:
TITLE: <at most 6 words, no quotes, no trailing period>
DESCRIPTION: <one sentence, at most 25 words, say what the call was about and the main outcome>"""


def describe(data, glossary=(), log=print):
    """-> (title, description) or (None, None) when there is not enough speech."""
    import re

    if data.get("summary"):
        body = "Minutes of the call:\n\n" + data["summary"]
    else:
        lines = [f"{common.label(data, u['speaker'])}: {u['text']}" for u in data["utterances"]]
        body = "Transcript:\n\n" + "\n".join(lines)[:8000]
        if len(body) < 80:
            return None, None
    model, tok = polish.get_model()
    gloss = ("Glossary of names and terms: " + ", ".join(glossary) + "\n\n") if glossary else ""
    out = _gen(model, tok, gloss + DESCRIBE.format(lang=language(data)) + "\n\n" + body, 120)
    t = re.search(r"TITLE:\s*(.+)", out)
    d = re.search(r"DESCRIPTION:\s*(.+)", out)
    if not t:
        return None, None
    return t.group(1).strip().strip('"'), (d.group(1).strip() if d else "")
