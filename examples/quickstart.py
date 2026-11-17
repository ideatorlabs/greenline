"""The README quickstart, runnable as is: python examples/quickstart.py"""

from greenline import Engine, MockProvider, Settings, Storage, TemplateDefinition, testing

storage = Storage("sqlite://")  # or "postgresql+psycopg://user:pass@host/db"
storage.create_all()
engine = Engine(storage, MockProvider(), Settings(provider="mock"))

# One tenant with its own sending number, and a template approved on its account.
engine.add_number(tenant_id="store-1", phone_number_id="PN-1", waba_id="WABA-1", token="access-token")
engine.submit_templates(
    "WABA-1",
    [
        TemplateDefinition(
            name="order_ready",
            category="utility",
            body="Hi {{1}}, your order {{2}} is ready. Thank you for shopping with us.",
        )
    ],
)


@engine.hooks.on("inbound")
def on_inbound(tenant_id: str, phone: str, text: str, **_: object) -> None:
    print(f"[{tenant_id}] {phone}: {text}")


# The customer has not written to us, so the approved template goes out.
r = engine.send(
    tenant_id="store-1",
    to="+1 555 0101",
    text="Your order is ready",
    template="order_ready",
    params=["Sam", "#1042"],
)
print(r.kind, r.status)  # template sent

# They reply (normally through the webhook), which opens the 24-hour window: free-form text now.
engine.process_webhook(testing.inbound_payload(phone_number_id="PN-1", phone="15550101", text="Thanks!"))
r = engine.send(tenant_id="store-1", to="+1 555 0101", text="You're welcome!", template="order_ready")
print(r.kind, r.status)  # text sent
