import json
from pathlib import Path

from groww_pulse.composition.composer import PROMPT_VERSION
from groww_pulse.composition.renderer import DOC_FORMAT


def write_artifacts(root: Path, run_id, pulse, report, model_ref):
    folder = root / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "pulse.md": pulse.rendered_body,
        "pulse.json": pulse.model_dump_json(indent=2),
        "evidence.json": json.dumps({"snapshot_id": str(pulse.snapshot_id),
            "quotes": [q.model_dump(mode="json") for q in pulse.quotes],
            "actions": [a.model_dump(mode="json") for a in pulse.actions],
            "prompt_version": PROMPT_VERSION, "model_ref": model_ref}, indent=2),
        "validation.json": json.dumps({**report, "document_format": DOC_FORMAT}, indent=2),
    }
    for name, text in files.items():
        temp = folder / (name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(folder / name)
