"""Deterministic validator child; never runs a model or changes Task state."""
import json
import sys
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.validation_runner import ValidationRunner


def run():
    source=Path(sys.argv[1]);payload=json.loads(source.read_bytes())
    result=ValidationRunner(KernelSettings.from_environment()).validate(payload['project_id'],payload['task_id'],
        payload['validation_policy'],payload['sealed_bundle'],report_directory=source.parent/'reports')
    encoded=json.dumps(result,ensure_ascii=False,sort_keys=True).encode()
    target=source.parent/'result.json';temporary=target.with_suffix('.tmp');temporary.write_bytes(encoded);temporary.replace(target)


if __name__=='__main__':run()
