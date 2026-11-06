"""Explicit installation of a public local transcription model."""

import argparse
import os
from pathlib import Path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="tiny", choices=["tiny", "base", "small", "medium", "large-v3"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from faster_whisper.utils import download_model
    result = download_model(args.model, output_dir=str(args.directory))
    print(f"Local audio model ready: {Path(result).resolve()}")
