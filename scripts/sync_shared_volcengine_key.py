from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


PLACEHOLDER_MARKERS = ("replace", "example", "placeholder", "secret", "your", "test")
DEFAULTS = {
    "schema_version": "shared-volcengine-models.v1",
    "provider": "volcengine",
    "region": "cn-beijing",
    "ark": {
        "enabled": True,
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": {
            "generation": "doubao-seed-2-0-mini-260428",
            "ocr": "doubao-seed-2-0-mini-260428",
            "embedding": "doubao-embedding-vision-251215",
            "video": "doubao-seedance-2-0-fast-260128",
            "embedding_dimensions": 1024,
        },
    },
    "las": {
        "enabled": False,
        "api_key": "",
        "endpoint": "https://operator.las.cn-beijing.volces.com/api/v1",
        "primary_resource": "seedasr",
        "backup_resource": "bigasr",
    },
    "rerank": {
        "enabled": False,
        "access_key": "",
        "secret_key": "",
        "endpoint": "https://api-knowledgebase.mlp.cn-beijing.volces.com",
        "region": "cn-north-1",
        "primary_model": "doubao-seed-rerank",
        "backup_model": "base-multilingual-rerank",
    },
}


def load_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def looks_placeholder(value: str) -> bool:
    lowered = value.casefold()
    return not value.strip() or any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def fill_missing(target: dict, defaults: dict) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in target or target[key] is None:
            target[key] = json.loads(json.dumps(value))
            changed = True
        elif isinstance(value, dict):
            if not isinstance(target[key], dict):
                raise ValueError(f"expected object at {key}")
            changed = fill_missing(target[key], value) or changed
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atomically copy the existing ordinary Ark API key into the shared config."
    )
    parser.add_argument(
        "--provider-secrets",
        type=Path,
        default=Path(
            os.getenv(
                "ASSISTANT_PROVIDER_SECRETS",
                "/home/v/AI/assistant-data/config/provider-secrets.json",
            )
        ),
    )
    parser.add_argument(
        "--shared-config",
        type=Path,
        default=Path(
            os.getenv(
                "ASSISTANT_SHARED_VOLCENGINE_CONFIG",
                "/home/v/AI/assistant-data/shared-models/volcengine.json",
            )
        ),
    )
    args = parser.parse_args()

    providers = load_object(args.provider_secrets)
    shared = load_object(args.shared_config)
    changed = fill_missing(shared, DEFAULTS)
    provider = ((providers.get("providers") or {}).get("volcengine") or {})
    source_key = str(provider.get("api_key") or "").strip()
    if not provider.get("enabled") or looks_placeholder(source_key):
        raise RuntimeError("enabled ordinary Volcengine provider key is unavailable")

    ark = shared.get("ark")
    if not isinstance(ark, dict):
        raise ValueError("shared Volcengine config has no ark object")
    current_key = str(ark.get("api_key") or "")
    if current_key == source_key and not changed:
        print("SHARED_VOLCENGINE_ARK_KEY_ALREADY_CURRENT")
        return
    if current_key != source_key and current_key and not looks_placeholder(current_key):
        raise RuntimeError("shared Ark key is non-placeholder and differs; refusing overwrite")

    if current_key != source_key:
        ark["api_key"] = source_key
        changed = True
    encoded = (json.dumps(shared, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = args.shared_config.with_suffix(args.shared_config.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(args.shared_config)
    print("SHARED_VOLCENGINE_CONFIG_SYNCED")


if __name__ == "__main__":
    main()
