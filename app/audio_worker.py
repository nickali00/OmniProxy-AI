"""Optional local CPU transcription, offline and without provider credentials."""

import argparse
import json
import resource
import wave


def main():
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("model")
    parser.add_argument("--language")
    args = parser.parse_args()
    try:
        import numpy as np
        from faster_whisper import WhisperModel
        # The parser subprocess already normalized this to mono 16 kHz PCM.
        # Decode the bounded WAV directly instead of invoking PyAV again.
        with wave.open(args.audio, "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
                raise ValueError("Unexpected normalized audio format")
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=2, num_workers=1, local_files_only=True)
        segments, information = model.transcribe(samples, beam_size=1, condition_on_previous_text=False, vad_filter=True, language=args.language)
        items = []
        size = 0
        for segment in segments:
            size += len(segment.text)
            if size > 250_000:
                raise ValueError("Transcript too large")
            items.append({"start": segment.start, "end": segment.end, "text": segment.text})
        result = {"text": " ".join(item["text"].strip() for item in items), "segments": items, "language": information.language}
    except ImportError:
        result = {"error": "audio_local_runtime_missing"}
    except Exception:  # noqa: BLE001 -- isolate native model errors without printing customer audio
        result = {"error": "audio_transcription_failed"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
