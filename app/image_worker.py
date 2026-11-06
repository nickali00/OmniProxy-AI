"""Optional offline CPU Diffusers worker; loads safetensors and library code only."""

import argparse
import json
import resource
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("output")
    parser.add_argument("model")
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    try:
        data = json.loads(Path(args.request).read_text())
        if data.get("mode") == "normalize":
            from PIL import Image
            for index in range(data["n"]):
                with Image.open(Path(args.output) / f"{index}.source", formats=["PNG", "JPEG", "WEBP"]) as image:
                    if image.size != (data["width"], data["height"]) or image.format not in {"PNG", "JPEG", "WEBP"}:
                        raise ValueError()
                    image.convert("RGB").save(Path(args.output) / f"{index}.png", format="PNG")
            print(json.dumps({"count": data["n"]}))
            return
        import torch
        from diffusers import AutoPipelineForText2Image

        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        model = Path(args.model)
        index = json.loads((model / "model_index.json").read_text())
        if index.get("_class_name") not in {"StableDiffusionPipeline", "StableDiffusionXLPipeline", "FluxPipeline", "Flux2Pipeline", "QwenImagePipeline", "ZImagePipeline"}:
            raise ValueError("Unsupported pipeline")
        pipeline = AutoPipelineForText2Image.from_pretrained(model, local_files_only=True, use_safetensors=True, torch_dtype=torch.float32)
        pipeline.to("cpu")
        pipeline.set_progress_bar_config(disable=True)
        for name in ("tokenizer", "tokenizer_2", "tokenizer_3"):
            tokenizer = getattr(pipeline, name, None)
            maximum = getattr(tokenizer, "model_max_length", 0)
            if tokenizer is not None and 0 < maximum <= 8192 and len(tokenizer(data["prompt"], truncation=False)["input_ids"]) > maximum:
                print(json.dumps({"error": "image_prompt_too_long"}))
                return
        result = pipeline(data["prompt"], width=data["width"], height=data["height"], num_inference_steps=data["steps"], num_images_per_prompt=data["n"], generator=torch.Generator(device="cpu").manual_seed(data["seed"]))
        if any(getattr(result, "nsfw_content_detected", None) or []):
            print(json.dumps({"error": "image_generation_filtered"}))
            return
        for index, image in enumerate(result.images):
            image.save(Path(args.output) / f"{index}.png", format="PNG")
        print(json.dumps({"count": len(result.images)}))
    except ImportError:
        print(json.dumps({"error": "image_local_runtime_missing"}))
    except Exception:  # noqa: BLE001 -- never expose prompts, model contents, or upstream internals
        print(json.dumps({"error": "image_generation_failed"}))


if __name__ == "__main__":
    main()
