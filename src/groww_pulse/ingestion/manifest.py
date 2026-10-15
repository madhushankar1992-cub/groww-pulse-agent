from pathlib import Path

from pydantic import ValidationError

from groww_pulse.config import read_yaml
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.schemas import ImportManifest


def load_manifest(path: Path) -> ImportManifest:
    try:
        return ImportManifest.model_validate(read_yaml(path, ErrorCode.MANIFEST_INVALID))
    except (ValidationError, ValueError):
        raise PulseError(ErrorCode.MANIFEST_INVALID) from None
