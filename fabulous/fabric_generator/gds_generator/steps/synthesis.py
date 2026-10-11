"""Verilog synthesis with an optional project-supplied Pyosys recipe."""

from pathlib import Path

from librelane.common import Path as ConfigPath
from librelane.config.variable import Variable
from librelane.steps.pyosys import Synthesis
from librelane.steps.step import Step, StepException


@Step.factory.register()
class FABulousSynthesis(Synthesis):
    """Run the standard synthesis recipe or a configured Python replacement."""

    id = "FABulous.Synthesis"
    config_vars = Synthesis.config_vars + [
        Variable(
            "FABULOUS_SYNTH_SCRIPT",
            ConfigPath | None,
            "Optional Python/Pyosys replacement for the Verilog synthesis script. "
            "Must accept LibreLane's synthesis arguments and emit its netlist "
            "and reports. Unset or null uses the standard LibreLane script.",
            default=None,
        ),
    ]

    def get_script_path(self) -> str:
        """Return the configured recipe, checking it is a Python file."""
        script = self.config["FABULOUS_SYNTH_SCRIPT"]
        if script is None:
            return super().get_script_path()

        path = Path(script).resolve()
        if not path.is_file() or path.suffix != ".py":
            raise StepException(
                f"FABULOUS_SYNTH_SCRIPT must reference an existing .py file: {path}"
            )
        return str(path)
