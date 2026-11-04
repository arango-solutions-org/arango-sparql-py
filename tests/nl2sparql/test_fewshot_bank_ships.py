"""The NL pipeline's few-shot bank ships in the package.

It lived under ``tests/``, which an installed wheel does not contain: every installed
deployment (the Docker images, BYOC, ``pip install``) ran the NL pipeline with no examples,
and a model then wrote Turtle-style ``PREFIX x: <...> .`` that does not parse. Only a source
checkout had them.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import arango_sparql
from arango_sparql.nl2sparql.engine_adapter import _FEWSHOT_BANK_PATH


def test_the_bank_is_inside_the_package_and_loads() -> None:
    package = Path(arango_sparql.__file__).resolve().parent
    assert _FEWSHOT_BANK_PATH.is_relative_to(package)
    examples = yaml.safe_load(_FEWSHOT_BANK_PATH.read_text())["examples"]
    assert len(examples) >= 15


def test_the_wheel_carries_it() -> None:
    """Hatch builds the wheel from ``packages = ["arango_sparql"]``: a file in the package
    directory that git tracks is in it, one outside is not."""
    import tomllib

    root = Path(arango_sparql.__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert any(_FEWSHOT_BANK_PATH.is_relative_to(root / p) for p in packages)
