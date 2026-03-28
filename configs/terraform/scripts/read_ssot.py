#!/usr/bin/env python3
import ast
import json
import pathlib
import sys

REQUIRED_KEYS = ("NUM_SERVERS", "PROJECT_NAME", "SSH_USER", "SSH_PASS")


def _load_constants(config_path):
    source = config_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(config_path))
    constants = {}

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue

        key = target.id
        if key not in REQUIRED_KEYS:
            continue

        try:
            constants[key] = ast.literal_eval(node.value)
        except Exception as exc:
            raise ValueError(
                f"Unable to parse {key} from {config_path}: {exc}"
            ) from exc

    missing = [key for key in REQUIRED_KEYS if key not in constants]
    if missing:
        raise ValueError(
            f"Missing required SSOT keys in {config_path}: {', '.join(missing)}"
        )

    if not isinstance(constants["NUM_SERVERS"], int) or constants["NUM_SERVERS"] < 0:
        raise ValueError("NUM_SERVERS must be an integer >= 0")

    return constants


def _emit(payload):
    print(json.dumps(payload))


def main():
    if len(sys.argv) != 2:
        _emit({"error": "Usage: read_ssot.py <path-to-sots-config.py>"})
        return 1

    config_path = pathlib.Path(sys.argv[1]).expanduser().resolve()
    if not config_path.exists():
        _emit({"error": f"SSOT config not found: {config_path}"})
        return 1

    try:
        constants = _load_constants(config_path)
    except Exception as exc:
        _emit({"error": str(exc)})
        return 1

    # External data source values must be strings.
    _emit(
        {
            "num_servers": str(constants["NUM_SERVERS"]),
            "project_name": str(constants["PROJECT_NAME"]),
            "ssh_user": str(constants["SSH_USER"]),
            "ssh_pass": str(constants["SSH_PASS"]),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
