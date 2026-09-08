"""Where adapters are found.

Explicit registration rather than entry-point discovery or a package scan. Two
reasons, and the second is the one that matters:

* An adapter that can be added by dropping a file on a path is an adapter that
  can be added by anyone who can write to that path. These run inside a worker
  holding database credentials.
* A registry you can read tells you what the platform integrates with. A
  discovery mechanism tells you to go and look.

Adding a network is two lines here and one new file. That is the whole cost, and
keeping it there is the framework's only real promise.
"""

from __future__ import annotations

from mmp_providers.base import Capability, Provider

_REGISTRY: dict[str, Provider] = {}


class UnknownProvider(KeyError):
    """Named for a provider nothing is registered under."""


def register(provider: Provider) -> Provider:
    """Add an adapter. Refuses to replace one silently.

    A duplicate name almost always means two adapters were written for the same
    network by different people, and whichever imported last would win — which
    is a very confusing way to find out.
    """
    if provider.name in _REGISTRY:
        raise ValueError(f"a provider named {provider.name!r} is already registered")
    if not provider.name.islower() or " " in provider.name:
        raise ValueError("provider names are lowercase and have no spaces")
    _REGISTRY[provider.name] = provider
    return provider


def get(name: str) -> Provider:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownProvider(
            f"no provider named {name!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from exc


def available() -> list[Provider]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def supporting(capability: Capability) -> list[Provider]:
    return [p for p in available() if capability in p.capabilities]


def _reset_for_tests() -> None:
    """Clear the registry. Only for tests that register their own adapters."""
    global _loaded
    _REGISTRY.clear()
    _loaded = False


_loaded = False


def load_builtin_once() -> None:
    """Register the built-in adapters, at most once.

    Called from consumer constructors rather than at import: a worker that
    creates several consumers would otherwise trip the duplicate-name guard,
    which exists to catch two adapters for one network and should not fire for
    this.
    """
    global _loaded
    if _loaded:
        return
    load_builtin()
    _loaded = True


def load_builtin() -> None:
    """Register the adapters that ship with the platform.

    Imported for the side effect, deliberately: the alternative is a list here
    that has to be kept in step with the files, and it never is.
    """
    from mmp_providers.adapters import custom, s2s_json  # noqa: F401
