"""Explicit download of public safetensors checkpoints at a pinned revision."""

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    if len(args.revision) != 40 or any(char not in "0123456789abcdef" for char in args.revision):
        parser.error("Indica una revisione Git completa e immutabile.")
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from huggingface_hub import snapshot_download

    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot_download(repo_id=args.repository, revision=args.revision, local_dir=args.directory, cache_dir=args.directory / ".cache", token=False, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "LICENSE*", "README.md"])
    print("Checkpoint pubblico preparato; nessun token Hugging Face utilizzato.")


if __name__ == "__main__":
    main()
