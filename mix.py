import sys
import wave

import numpy as np

out = sys.argv[1]
tracks = []
rate = 48000
for p in sys.argv[2:]:
    with wave.open(p) as w:
        rate = w.getframerate()
        tracks.append(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32))

n = max(len(t) for t in tracks)
mix = np.zeros(n, dtype=np.float32)
for t in tracks:
    mix[: len(t)] += t

peak = float(np.abs(mix).max())
if peak > 32767:
    mix *= 32767 / peak

with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(mix.astype(np.int16).tobytes())
