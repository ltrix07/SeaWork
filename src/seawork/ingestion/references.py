from pathlib import Path
from typing import Any, cast

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Reference file must contain a mapping: {path}")
    return cast(dict[str, Any], value)
