"""Greenline: a self-hostable, multi-tenant engine for the WhatsApp Business Cloud API.

from greenline import Engine, Settings, Storage

storage = Storage("sqlite:///greenline.db")
storage.create_all()
engine = Engine(storage, settings=Settings(provider="mock"))
"""

from __future__ import annotations

from greenline.billing import RateCard, budget_status
from greenline.config import Settings
from greenline.consent import keyword_action
from greenline.engine import POLICY_SKIPS, Engine, status_advances
from greenline.errors import GreenlineError, NotConfigured, NumberNotReady, ProviderError, RateLimited
from greenline.hooks import Hooks
from greenline.params import clean_param, fit_params
from greenline.providers import (
    MetaCloudProvider,
    MockProvider,
    Provider,
    build_provider,
    register_provider,
)
from greenline.schemas import (
    Outbound,
    ProviderResult,
    Sender,
    SendResult,
    TemplateMessage,
    WebhookEvent,
)
from greenline.security import TokenCipher, generate_key, sign, verify_signature
from greenline.storage import Storage, build_models, create_all
from greenline.templates import Button, TemplateDefinition, TemplateRegistry, normalise_status
from greenline.webhook import WebhookHandler

__version__ = "0.1.0"

__all__ = [
    "POLICY_SKIPS",
    "Button",
    "GreenlineError",
    "Engine",
    "Hooks",
    "MetaCloudProvider",
    "MockProvider",
    "NotConfigured",
    "NumberNotReady",
    "Outbound",
    "Provider",
    "ProviderError",
    "ProviderResult",
    "RateCard",
    "RateLimited",
    "SendResult",
    "Sender",
    "Settings",
    "Storage",
    "TemplateDefinition",
    "TemplateMessage",
    "TemplateRegistry",
    "TokenCipher",
    "WebhookEvent",
    "WebhookHandler",
    "__version__",
    "budget_status",
    "build_models",
    "build_provider",
    "clean_param",
    "create_all",
    "fit_params",
    "generate_key",
    "keyword_action",
    "normalise_status",
    "register_provider",
    "sign",
    "status_advances",
    "verify_signature",
]
