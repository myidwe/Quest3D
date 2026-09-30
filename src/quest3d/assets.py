"""Explicit, checksum-verified model setup. Inference never downloads assets."""

import hashlib
import json
import urllib.request
import subprocess
from pathlib import Path

from .paths import MODEL_DIR, ROOT
from .model_choice import DEFAULT_DEPTH_MODEL as DEFAULT_MODEL_ID, DEPTH_MODEL_IDS


def model_spec(model_id: str = DEFAULT_MODEL_ID, *, root: Path | None = None) -> dict:
    if not isinstance(model_id, str) or model_id not in DEPTH_MODEL_IDS:
        raise ValueError(f"Unknown depth model: {model_id!r}")
    models = json.loads(((root or ROOT) / "config/models.json").read_text("utf-8-sig"))
    if model_id not in models:
        raise ValueError(f"Depth model installation metadata is missing: {model_id}")
    return models[model_id]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_model_source(root: Path = ROOT, *, model_id: str = DEFAULT_MODEL_ID) -> Path:
    """Public packages use exact source hashes, without shipping a Git checkout."""
    source = root / 'third_party/depth-anything-v2'
    selected = model_spec(model_id, root=root)
    # DAD Small is verified against this exact DPT architecture. Its training
    # repository remains separately recorded; it is not an inference dependency.
    spec = model_spec(selected.get('runtime_source_model_id', model_id), root=root)
    manifest = root / 'config/depth-source.json'
    if manifest.exists():
        data = json.loads(manifest.read_text('utf-8-sig'))
        files = data.get('files')
        if data.get('source_commit') != spec['source_commit'] or not isinstance(files, dict) or not files:
            raise ValueError('모델 소스 설치 정보가 고정 버전과 다릅니다.')
        actual = {p.relative_to(source).as_posix() for p in (source / 'depth_anything_v2').rglob('*.py')}
        python_files = {name for name in files if name.endswith('.py')}
        if not actual or actual != python_files or 'depth_anything_v2/dpt.py' not in files:
            raise ValueError('모델 소스 파일이 누락되거나 추가되었습니다. 설치 파일로 복구해 주세요.')
        for name, expected in files.items():
            path = (source / name).resolve()
            if not path.is_relative_to(source.resolve()) or not path.is_file() or sha256_file(path) != expected:
                raise ValueError('모델 소스 체크섬이 다릅니다. 설치 파일로 복구해 주세요.')
    else:
        if not (source / 'depth_anything_v2/dpt.py').is_file():
            raise FileNotFoundError('모델 소스가 없습니다. 설치 도구로 복구해 주세요.')
        revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        if revision != spec['source_commit']:
            raise RuntimeError('The model source revision differs from config/models.json')
    return source


def verified_model(path: Path | None = None, *, model_id: str = DEFAULT_MODEL_ID) -> Path:
    spec = model_spec(model_id)
    path = Path(path) if path is not None else MODEL_DIR / spec["filename"]
    if not path.is_file():
        selection = "" if model_id == DEFAULT_MODEL_ID else f" --model-id {model_id}"
        raise FileNotFoundError(f"Model is missing ({model_id}). Run: uv run --locked quest3d setup-model{selection}")
    if path.stat().st_size != spec["bytes"] or sha256_file(path) != spec["sha256"]:
        raise ValueError(f"Model checksum mismatch: {path}")
    return path


def setup_model(model_id: str = DEFAULT_MODEL_ID) -> dict:
    spec = model_spec(model_id)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    target = MODEL_DIR / spec["filename"]
    if target.exists():
        verified_model(target, model_id=model_id)
        return {"path": str(target), "sha256": spec["sha256"], "downloaded": False, "model_id": model_id}
    partial = target.with_suffix(target.suffix + ".part")
    remote = spec.get("repository_filename", spec["filename"])
    url = f"https://huggingface.co/{spec['repository']}/resolve/{spec['revision']}/{remote}"
    request = urllib.request.Request(url, headers={"User-Agent": "Quest3D-model-setup/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            output.write(chunk)
    verified_model(partial, model_id=model_id)
    partial.replace(target)
    receipt = {**spec, "model_id": model_id, "path": str(target), "downloaded": True, "local_hash_verified": True}
    receipt_name = "receipt.json" if model_id == DEFAULT_MODEL_ID else f"receipt-{model_id}.json"
    (MODEL_DIR / receipt_name).write_text(json.dumps(receipt, indent=2), "utf-8")
    return receipt
