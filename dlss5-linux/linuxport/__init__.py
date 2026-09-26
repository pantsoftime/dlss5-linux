"""Linux backends for the upstream DLSS5-Autopilot core. Call activate() once."""
from . import linux_gpu, games, paths, pe, policy, state, pins, tuning, diagnosefix, components, nrlinux, feederpick


def activate() -> None:
    paths.install()
    linux_gpu.install()
    games.install()
    pe.install()
    policy.install()
    state.install()
    pins.install()
    tuning.install()
    diagnosefix.install()
    components.install()
    nrlinux.install()
    feederpick.install()
