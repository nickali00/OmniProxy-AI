"""Bounded offline speech renderer; no provider credentials or network input."""

import argparse
import json
import os
import resource
import subprocess
import wave
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("output")
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    try:
        data = json.loads(Path(args.request).read_text())
        if data["voice"] not in {"it", "en-us", "en-gb", "es", "fr", "de"} or data["format"] not in {"wav", "mp3", "flac", "opus", "pcm", "aac"}:
            raise ValueError()
        raw = Path(args.output).with_suffix(".raw.wav")
        pulse = Path(args.output).with_suffix(".pulse.conf")
        pulse.write_text("enable-shm = no\nenable-memfd = no\nautospawn = no\n")
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PULSE_CLIENTCONFIG": str(pulse), "PULSE_SERVER": "unix:/nonexistent"}
        generated = subprocess.run(["espeak-ng", "--stdin", "-v", data["voice"], "-s", str(round(175 * data["speed"])), "--stdout"], input=data["input"].encode(), capture_output=True, timeout=20, check=True, env=environment)
        if not 0 < len(generated.stdout) <= 16 * 1024 * 1024:
            raise ValueError()
        raw.write_bytes(generated.stdout)
        with wave.open(str(raw), "rb") as audio:
            # espeak's stdout WAV uses a provisional frame count. Count actual
            # bounded PCM bytes rather than trusting that placeholder header.
            frames = audio.readframes(16 * 1024 * 1024)
            duration = len(frames) / (audio.getsampwidth() * audio.getnchannels() * audio.getframerate())
        logical_duration = min(duration, data["max_seconds"])
        codec, container = {"wav": ("pcm_s16le", "wav"), "mp3": ("libmp3lame", "mp3"), "flac": ("flac", "flac"), "opus": ("libopus", "ogg"), "pcm": ("pcm_s16le", "s16le"), "aac": ("aac", "adts")}[data["format"]]
        subprocess.run(["ffmpeg", "-v", "error", "-protocol_whitelist", "file,pipe", "-threads", "2", "-i", str(raw), "-t", str(logical_duration), "-ac", "1", "-ar", "24000", "-c:a", codec, "-filter_threads", "1", "-threads", "2", "-f", container, "-y", args.output], capture_output=True, timeout=20, check=True)
        print(json.dumps({"duration_seconds": logical_duration, "truncated": logical_duration < duration}))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, wave.Error):
        print(json.dumps({"error": "speech_generation_failed"}))


if __name__ == "__main__":
    main()
