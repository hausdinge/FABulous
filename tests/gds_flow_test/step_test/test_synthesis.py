"""Tests for configurable Pyosys synthesis without running an ASIC flow."""

import json
from pathlib import Path

import pytest
from librelane.common import GenericDict
from librelane.config.config import Config
from librelane.state.state import State
from librelane.steps.pyosys import Synthesis
from librelane.steps.step import Step, StepException
from pytest_mock import MockerFixture

from fabulous.fabric_generator.gds_generator.flows.flow_define import prep_steps
from fabulous.fabric_generator.gds_generator.steps.synthesis import FABulousSynthesis


def test_default_script_and_registration() -> None:
    """The new flow step preserves the stock recipe and stock registration."""
    step = FABulousSynthesis()
    step.config = Config({"FABULOUS_SYNTH_SCRIPT": None})
    assert step.get_script_path() == Synthesis.get_script_path(step)
    assert FABulousSynthesis in prep_steps
    assert Step.factory.get("FABulous.Synthesis") is FABulousSynthesis
    assert Step.factory.get("Yosys.Synthesis") is Synthesis


@pytest.mark.parametrize("invalid", ["missing.py", "recipe.tcl", "directory.py"])
def test_invalid_script(tmp_path: Path, invalid: str) -> None:
    """Missing files, directories and non-Python scripts fail explicitly."""
    path = tmp_path / invalid
    if invalid == "directory.py":
        path.mkdir()
    elif invalid == "recipe.tcl":
        path.touch()
    step = FABulousSynthesis()
    step.config = Config({"FABULOUS_SYNTH_SCRIPT": str(path)})
    with pytest.raises(StepException, match="existing .py file"):
        step.get_script_path()


@pytest.mark.parametrize("setting", ["omitted", "null", "file", "missing"])
def test_script_variable(tmp_path: Path, setting: str) -> None:
    """LibreLane validates the optional path and defaults it to null."""
    variable = FABulousSynthesis.config_vars[-1]
    values = {}
    path = tmp_path / "recipe.py"
    if setting == "file":
        path.touch()
    if setting != "omitted":
        values[variable.name] = None if setting == "null" else str(path)
    if setting == "missing":
        with pytest.raises(ValueError, match="invalid"):
            variable.compile(GenericDict(values), [])
    else:
        _, result = variable.compile(GenericDict(values), [])
        assert (
            result is None
            if setting in {"omitted", "null"}
            else str(result) == str(path)
        )


@pytest.mark.parametrize("custom", [False, True])
def test_synthesis_command(tmp_path: Path, mocker: MockerFixture, custom: bool) -> None:
    """Both recipes receive the same configuration, libraries and output args."""
    script = tmp_path / "custom recipe.py"
    script.touch()
    excluded_cells = tmp_path / "excluded.txt"
    excluded_cells.touch()
    step = FABulousSynthesis()
    step.config = Config(
        {
            "FABULOUS_SYNTH_SCRIPT": str(script) if custom else None,
            "DESIGN_NAME": "tile",
            "YOSYS_LOG_LEVEL": "ALL",
            "CELL_LIBS": {},
            "PAD_LIBS": {},
            "EXTRA_EXCLUDED_CELLS": [],
            "SYNTH_EXCLUDED_CELL_FILE": str(excluded_cells),
            "PNR_EXCLUDED_CELL_FILE": str(excluded_cells),
        }
    )
    step.step_dir = str(tmp_path)
    step.toolbox = mocker.Mock()
    step.toolbox.filter_views.side_effect = [["cells.lib"], []]
    step.toolbox.get_macro_views_by_priority.return_value = []
    step.toolbox.remove_cells_from_lib.return_value = ["filtered.lib"]
    command = step.get_command(State())
    expected = str(script) if custom else Synthesis.get_script_path(step)
    assert command[1:3] == ["-y", expected]
    assert command[command.index("--config-in") + 1] == str(tmp_path / "config.json")
    assert command[command.index("--extra-in") + 1] == str(tmp_path / "extra.json")
    assert command[command.index("--output") + 1] == str(tmp_path / "tile.nl.v")
    extra = json.loads((tmp_path / "extra.json").read_text())
    assert extra == {"blackbox_models": ["cells.lib"], "libs_synth": ["filtered.lib"]}
