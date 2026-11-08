from importlib import import_module

_MODULES = ("health", "climate", "prosperity", "food")

SAMPLES = []
EXAMPLES = []
for _name in _MODULES:
    try:
        _m = import_module(f"{__name__}.{_name}")
    except ModuleNotFoundError:
        continue
    SAMPLES.append(_m.SAMPLE)
    EXAMPLES.extend(_m.EXAMPLES)
