# Extending

Add a provider (for example a BSP) by implementing four methods:

```python
from greenline import Outbound, ProviderResult, Sender, register_provider
from greenline.providers.meta_cloud import parse_webhook_payload


class MyBspProvider:
    name = "my_bsp"

    def send(self, sender: Sender, message: Outbound) -> ProviderResult:
        ...  # call the BSP; raise ProviderError(retryable=...) or RateLimited on failure
        return ProviderResult(ok=True, provider_message_id="...", status="sent")

    def parse_webhook(self, payload: dict) -> list:
        return parse_webhook_payload(payload)  # if the BSP forwards Meta's format

    def create_template(self, waba_id, token, *, name, language, category, components) -> dict: ...

    def list_templates(self, waba_id, token) -> list[dict]: ...


register_provider("my_bsp", lambda settings, **_: MyBspProvider())
```

Other frameworks: `engine.webhook.verify(query_params)` and
`engine.webhook.receive(raw_body, signature_header)` return `(status, body)`; wrap them in a
Flask, Django or Starlette view in a few lines. Use `engine.storage.metadata` as Alembic's
`target_metadata` if you manage migrations.
