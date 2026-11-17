"""Provider adapters and a small registry to pick one by name."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from greenline.config import Settings
from greenline.providers.base import Provider
from greenline.providers.meta_cloud import MetaCloudProvider
from greenline.providers.mock import MockProvider

ProviderFactory = Callable[..., Provider]

_REGISTRY: dict[str, ProviderFactory] = {
    "mock": lambda settings, **_: MockProvider(),
    "meta_cloud": lambda settings, **kw: MetaCloudProvider(settings, **kw),
}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Make ``Settings(provider=name)`` build your provider. ``factory(settings, **kwargs)``."""
    _REGISTRY[name] = factory


def build_provider(settings: Settings, **kwargs: Any) -> Provider:
    """The provider named by ``settings.provider``."""
    try:
        factory = _REGISTRY[settings.provider]
    except KeyError:
        raise ValueError(f"unknown provider {settings.provider!r}; known: {sorted(_REGISTRY)}") from None
    return factory(settings, **kwargs)


__all__ = ["MetaCloudProvider", "MockProvider", "Provider", "build_provider", "register_provider"]
