import json

FIELDS = {"run_id", "node", "status", "reason_code", "duration_seconds", "usage"}


def emit(path, **event):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({k: v for k, v in event.items() if k in FIELDS}) + "\n")
