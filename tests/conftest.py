"""Keep test scratch files inside this project, without touching global temp data."""
from pathlib import Path
import uuid


def pytest_configure(config):
    if config.option.basetemp is None:
        root = Path(__file__).resolve().parents[1]
        scratch = root / ".cache" / "pytest" / uuid.uuid4().hex
        if not scratch.resolve().is_relative_to(root) or scratch.exists():
            raise RuntimeError("Refusing an unsafe or existing test scratch directory")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(scratch)
