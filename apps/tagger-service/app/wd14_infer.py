from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from PIL import Image, ImageOps


RATING_NAMES = {
    "general": "general",
    "sensitive": "sensitive",
    "questionable": "questionable",
    "explicit": "explicit",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, default=Path("assets_100.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("model_tags_100.jsonl"))
    parser.add_argument("--model-repo", default="SmilingWolf/wd-vit-tagger-v3")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--tag-threshold", type=float, default=0.35)
    parser.add_argument("--character-threshold", type=float, default=0.35)
    parser.add_argument("--max-tags", type=int, default=80)
    args = parser.parse_args()

    if args.model_dir is not None:
        model_path = args.model_dir / "model.onnx"
        tags_path = args.model_dir / "selected_tags.csv"
    else:
        model_path = hf_hub_download(args.model_repo, "model.onnx")
        tags_path = hf_hub_download(args.model_repo, "selected_tags.csv")
    tags = read_tags(Path(tags_path))

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    session = ort.InferenceSession(model_path, providers=providers)
    input_meta = session.get_inputs()[0]
    input_name = input_meta.name
    input_shape = input_meta.shape
    size = infer_size(input_shape)
    nchw = len(input_shape) == 4 and input_shape[1] == 3

    rows = [json.loads(line) for line in args.assets.read_text("utf-8").splitlines() if line.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            image_path = args.assets.parent / row["sample_path"]
            tensor = preprocess_image(image_path, size=size, nchw=nchw)
            output = session.run(None, {input_name: tensor})[0][0].astype(np.float32)
            scores = normalize_scores(output)
            payload = build_payload(
                row,
                tags=tags,
                scores=scores,
                model=args.model_repo,
                tag_threshold=args.tag_threshold,
                character_threshold=args.character_threshold,
                max_tags=args.max_tags,
            )
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    print(f"wrote {len(rows)} rows to {args.out}")
    print(f"providers={session.get_providers()} input_shape={input_shape}")
    return 0


def read_tags(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append({"name": row["name"], "category": int(row["category"])})
    return rows


def infer_size(shape: list[object]) -> int:
    ints = [value for value in shape if isinstance(value, int) and value > 3]
    return int(ints[0]) if ints else 448


def preprocess_image(path: Path, *, size: int, nchw: bool) -> np.ndarray:
    image = Image.open(path)
    image = ImageOps.exif_transpose(image)
    if image.mode == "RGBA":
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        background.alpha_composite(image)
        image = background.convert("RGB")
    else:
        image = image.convert("RGB")

    width, height = image.size
    square = max(width, height)
    canvas = Image.new("RGB", (square, square), (255, 255, 255))
    canvas.paste(image, ((square - width) // 2, (square - height) // 2))
    canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
    array = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
    if nchw:
        array = np.transpose(array, (2, 0, 1))
    return np.expand_dims(array, axis=0)


def normalize_scores(scores: np.ndarray) -> np.ndarray:
    if float(scores.min()) < 0.0 or float(scores.max()) > 1.0:
        return 1.0 / (1.0 + np.exp(-scores))
    return scores


def build_payload(
    row: dict[str, object],
    *,
    tags: list[dict[str, object]],
    scores: np.ndarray,
    model: str,
    tag_threshold: float,
    character_threshold: float,
    max_tags: int,
) -> dict[str, object]:
    rating: dict[str, float] = {}
    selected: list[dict[str, object]] = []
    for tag, score in zip(tags, scores, strict=False):
        name = str(tag["name"])
        category = int(tag["category"])
        value = round(float(score), 6)
        if category == 9:
            rating[RATING_NAMES.get(name, name)] = value
        elif category == 4:
            if score >= character_threshold:
                selected.append({"tag": name, "score": value, "category": "character"})
        elif score >= tag_threshold:
            selected.append({"tag": name, "score": value, "category": "general"})
    selected.sort(key=lambda item: float(item["score"]), reverse=True)
    return {
        "pixiv_addr": row.get("pixiv_addr"),
        "local_path": row.get("local_path"),
        "sample_path": row.get("sample_path"),
        "key": row.get("key"),
        "model": model,
        "tags": selected[:max_tags],
        "rating": rating,
    }


if __name__ == "__main__":
    raise SystemExit(main())
