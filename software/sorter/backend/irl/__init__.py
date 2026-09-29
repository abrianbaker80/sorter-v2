"""Load hardware-facing compatibility exports only when explicitly requested."""

from importlib import import_module

__all__ = [
    "IRLConfig", "IRLInterface", "mkIRLConfig", "mkIRLInterface",
    "StepperMotor", "ServoMotor",
]

_EXPORT_MODULES = {
    **dict.fromkeys(__all__[:4], "irl.config"),
    **dict.fromkeys(__all__[4:], "hardware.sorter_interface"),
}


def __getattr__(name):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
