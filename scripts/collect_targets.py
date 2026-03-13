#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def load_event_payload() -> dict:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    try:
        return json.loads(Path(event_path).read_text())
    except Exception:
        return {}


def load_configs(config_root: Path) -> tuple[list[dict], dict[str, dict]]:
    index = json.loads((config_root / "repos" / "index.json").read_text())
    configs: list[dict] = []
    by_repo: dict[str, dict] = {}
    for file_name in index.get("repos", []):
        config_path = config_root / "repos" / file_name
        config = json.loads(config_path.read_text())
        config["_config_file"] = file_name
        configs.append(config)
        by_repo[config["repo"]] = config
    return configs, by_repo


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> int:
    config_root = Path(os.environ["CONFIG_ROOT"]).resolve()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event = load_event_payload()
    payload = event.get("client_payload", {}) if isinstance(event, dict) else {}

    configs, by_repo = load_configs(config_root)

    target_repo = (
        os.environ.get("INPUT_TARGET_REPO")
        or str(payload.get("target_repo", "")).strip()
    )
    target_version = (
        os.environ.get("INPUT_TARGET_VERSION")
        or str(payload.get("target_version", "")).strip()
    )
    force_build = parse_bool(
        os.environ.get("INPUT_FORCE_BUILD"),
        parse_bool(payload.get("force_build"), False),
    )
    publish_to_store = parse_bool(
        os.environ.get("INPUT_PUBLISH_TO_STORE"),
        parse_bool(payload.get("publish_to_store"), False),
    )
    check_only = parse_bool(
        os.environ.get("INPUT_CHECK_ONLY"),
        parse_bool(payload.get("check_only"), False),
    )

    selected: list[dict] = []
    if target_repo:
        config = by_repo.get(target_repo)
        if not config:
            print(f"Config not found for target repo: {target_repo}", file=sys.stderr)
            return 1
        selected.append(config)
    else:
        if event_name not in {"schedule", "workflow_dispatch"}:
            print("No target repo provided for non-schedule event", file=sys.stderr)
            return 1
        selected.extend(config for config in configs if parse_bool(config.get("enabled"), True))

    matrix = []
    for config in selected:
        matrix.append(
            {
                "target_repo": config["repo"],
                "config_file": config["_config_file"],
                "target_version": target_version,
                "force_build": force_build,
                "publish_to_store": publish_to_store,
                "check_only": check_only,
            }
        )

    payload_json = json.dumps(matrix, separators=(",", ":"))
    write_output("matrix", payload_json)
    write_output("has_targets", "true" if matrix else "false")
    print(payload_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
