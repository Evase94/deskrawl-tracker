"""Build data/legendary.wav: a short bright chime (three rising bell tones) played when a Legendary drops.
Synthesised here so the tracker ships no third-party audio."""
import os
import wave

import numpy as np

RATE = 44100
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "legendary.wav")


def bell(freq, start, length, total):
    t = np.arange(int(length * RATE)) / RATE
    env = np.exp(-t * 5.0) * np.minimum(t / 0.004, 1.0)
    tone = (np.sin(2 * np.pi * freq * t) + 0.45 * np.sin(2 * np.pi * freq * 2.01 * t)
            + 0.2 * np.sin(2 * np.pi * freq * 3.02 * t) * np.exp(-t * 9))
    out = np.zeros(total)
    i = int(start * RATE)
    out[i:i + len(t)] += (tone * env)[:total - i]
    return out


def main():
    total = int(1.3 * RATE)
    s = bell(880.0, 0.0, 0.9, total) + bell(1108.7, 0.11, 0.9, total) + bell(1318.5, 0.22, 1.05, total)
    s = s / np.abs(s).max() * 0.7
    with wave.open(OUT, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((s * 32767).astype("<i2").tobytes())
    print(OUT)


if __name__ == "__main__":
    main()
