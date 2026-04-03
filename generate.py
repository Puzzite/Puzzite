import argparse
import torch
from diffusers import StableDiffusionPipeline, DPMSolverMultistepScheduler
from pathlib import Path


def load_pipeline(model_id: str, device: str) -> StableDiffusionPipeline:
    pipe = StableDiffusionPipeline.from_pretrained(
        model_id,
        torch_dtype=torch.float16 if device != "cpu" else torch.float32,
        safety_checker=None,
    )
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    pipe = pipe.to(device)
    if device == "cuda":
        pipe.enable_attention_slicing()
    return pipe


def generate(
    pipe: StableDiffusionPipeline,
    prompt: str,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    steps: int = 20,
    guidance_scale: float = 7.5,
    seed: int | None = None,
    num_images: int = 1,
) -> list:
    generator = torch.Generator(pipe.device)
    if seed is not None:
        generator = generator.manual_seed(seed)

    result = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt or None,
        width=width,
        height=height,
        num_inference_steps=steps,
        guidance_scale=guidance_scale,
        generator=generator,
        num_images_per_prompt=num_images,
    )
    return result.images


def main():
    parser = argparse.ArgumentParser(description="Stable Diffusion image generation")
    parser.add_argument("prompt", help="Text prompt for generation")
    parser.add_argument("--negative", default="", help="Negative prompt")
    parser.add_argument("--model", default="stabilityai/stable-diffusion-2-1", help="HuggingFace model ID")
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--guidance", type=float, default=7.5)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--num-images", type=int, default=1)
    parser.add_argument("--output-dir", default="outputs", help="Directory to save images")
    parser.add_argument("--device", default=None, help="Device: cuda, mps, or cpu (auto-detected if omitted)")
    args = parser.parse_args()

    if args.device:
        device = args.device
    elif torch.cuda.is_available():
        device = "cuda"
    elif torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"

    print(f"Using device: {device}")
    print(f"Loading model: {args.model}")
    pipe = load_pipeline(args.model, device)

    print(f"Generating {args.num_images} image(s)...")
    images = generate(
        pipe,
        prompt=args.prompt,
        negative_prompt=args.negative,
        width=args.width,
        height=args.height,
        steps=args.steps,
        guidance_scale=args.guidance,
        seed=args.seed,
        num_images=args.num_images,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, image in enumerate(images):
        suffix = f"_{i}" if len(images) > 1 else ""
        seed_tag = f"_seed{args.seed}" if args.seed is not None else ""
        filename = output_dir / f"output{suffix}{seed_tag}.png"
        image.save(filename)
        print(f"Saved: {filename}")


if __name__ == "__main__":
    main()
