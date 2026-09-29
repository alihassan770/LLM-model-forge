"""The model providers this platform can talk to, and what each one can do.

Capabilities are declared here rather than discovered at runtime because the
pipeline has to make decisions before it spends money - refusing to run a photo
extraction on a text-only model is cheaper and clearer than letting the call
fail halfway through a job.

`image_tokens` is the detail budget the provider gives a single image. It is the
number that matters most for this product: reading "1800" off a dimension label
in a photograph is a resolution problem, and a model that sees an image at 384
tokens is not seeing the same picture as one that sees it at 4784.

Everything here is copied from the providers' own documentation on the date in
`CHECKED`, with the source URL beside each provider. It goes stale: models are
added, prices change, and a default flips. `python run.py check-models` fetches
those pages and reports what no longer matches, so the drift is found on
purpose rather than by a support ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# When the figures below were last read from the providers' documentation.
CHECKED = "2026-09-17"


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    vision: bool
    image_tokens: int          # detail budget per image; 0 when text-only
    context: int
    price_in: float            # USD per 1M input tokens
    price_out: float           # USD per 1M output tokens
    note: str = ""
    # How much room an extraction gets to answer in. A spec for a sideboard
    # came to seventeen parts and ran past 16k output tokens, which the
    # provider reports as a normal stop - so the ceiling has to be set where a
    # thorough answer fits, not where a typical one does. Costs nothing when
    # unused: output is billed on what is produced.
    max_output: int = 32_000
    # Whether the model can think before answering, and at what settings. Both
    # DeepSeek models do, and it is on by default at their end - which is worth
    # knowing, because it means an extraction has been using reasoning all
    # along without this app ever asking for it. Sending the setting explicitly
    # makes the behaviour ours rather than a default that can change.
    reasoning: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    default_effort: str = ""
    # Whether the provider can be held to a JSON Schema rather than merely
    # asked for JSON. True means we get schema-valid output or a clear failure;
    # false means we state the schema in the prompt and validate on return.
    structured: bool = False


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    key_env: str
    key_prefix: str            # shown as a hint; not used for validation
    docs_url: str
    base_url: str | None
    models: list[Model] = field(default_factory=list)
    # A dynamic provider's models are fetched from the provider rather than
    # written here. Only OpenRouter, which carries hundreds of models from
    # every lab and changes them weekly - see `openrouter.py` for why that
    # cannot be a hand-maintained list.
    dynamic: bool = False

    def catalog(self) -> list[Model]:
        """Every model this provider offers. Never makes a request.

        A dynamic provider returns whatever was last fetched, which may be
        empty on a cold start. Warming it is the caller's job - the settings
        page does it - so that an unreachable provider can never hang a page
        that had nothing to do with it.
        """
        if not self.dynamic:
            return self.models
        from . import openrouter
        return openrouter.cached()

    def model(self, model_id: str) -> Model | None:
        found = next((m for m in self.catalog() if m.id == model_id), None)
        if found is not None or not self.dynamic or not model_id:
            return found
        # Not in our copy of the catalogue. On a dynamic provider that means
        # "added since we last looked" at least as often as it means "wrong",
        # and refusing a model the provider will happily serve is the worse
        # error of the two.
        from . import openrouter
        return openrouter.synthesize(model_id)

    @property
    def default_model(self) -> Model:
        # The first vision-capable model, because every stage of this pipeline
        # that matters starts by looking at a picture.
        models = self.catalog()
        if not models:
            # A dynamic provider with a cold cache. Name the model rather than
            # crash; it resolves for real once the catalogue is fetched.
            from . import openrouter
            return openrouter.synthesize(DEFAULT_DYNAMIC_MODEL)
        return next((m for m in models if m.vision), models[0])


# What a dynamic provider falls back to before its catalogue has been
# fetched. Only ever shown for the moment between choosing OpenRouter and the
# list arriving; it is a real id, so it also works if the fetch never does.
DEFAULT_DYNAMIC_MODEL = "anthropic/claude-sonnet-4.5"

PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        id="anthropic",
        label="Claude (Anthropic)",
        key_env="ANTHROPIC_API_KEY",
        key_prefix="sk-ant-",
        docs_url="https://platform.claude.com/",
        base_url=None,
        models=[
            Model(
                id="claude-opus-5",
                label="Claude Opus 5",
                vision=True,
                structured=True,
                image_tokens=4784,
                context=1_000_000,
                price_in=5.00,
                price_out=25.00,
                note="Highest fidelity on drawings and dimension labels.",
            ),
            Model(
                id="claude-sonnet-5",
                label="Claude Sonnet 5",
                vision=True,
                structured=True,
                image_tokens=4784,
                context=1_000_000,
                price_in=3.00,
                price_out=15.00,
                note="Near-Opus quality at lower cost.",
            ),
            Model(
                id="claude-haiku-4-5",
                label="Claude Haiku 4.5",
                vision=True,
                structured=True,
                image_tokens=1568,
                context=200_000,
                price_in=1.00,
                price_out=5.00,
                note="Fast and cheap. Best kept for page selection, not extraction.",
            ),
        ],
    ),
    "deepseek": Provider(
        id="deepseek",
        label="DeepSeek",
        key_env="DEEPSEEK_API_KEY",
        key_prefix="sk-",
        docs_url="https://api-docs.deepseek.com/api/create-chat-completion",
        base_url="https://api.deepseek.com",
        # Prices are the *peak* rate. DeepSeek halves them outside 01:00-04:00
        # and 06:00-10:00 UTC on weekdays, so a cost shown to the user is an
        # upper bound rather than a surprise. Cache hits are cheaper again
        # (~$0.006/1M), which is not modelled: understating a bill is worse
        # than overstating one.
        models=[
            Model(
                id="deepseek-flash",
                label="DeepSeek Flash",
                vision=True,
                image_tokens=384,
                context=1_000_000,
                price_in=0.30,
                price_out=1.20,
                max_output=64_000,
                reasoning=True,
                reasoning_efforts=("none", "low", "high", "max"),
                default_effort="high",
                note="Cheap and reads images, but sees each one at ~12x less "
                     "detail than Claude.",
            ),
            Model(
                id="deepseek-v4-pro",
                label="DeepSeek V4 Pro",
                vision=False,
                image_tokens=0,
                context=1_000_000,
                price_in=1.32,
                price_out=3.96,
                max_output=64_000,
                reasoning=True,
                reasoning_efforts=("none", "low", "high", "max"),
                default_effort="high",
                note="Text only - cannot read photos or PDFs, so it cannot be "
                     "used for extraction.",
            ),
        ],
    ),
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter",
        key_env="OPENROUTER_API_KEY",
        key_prefix="sk-or-v1-",
        docs_url="https://openrouter.ai/docs/api-reference/overview",
        base_url="https://openrouter.ai/api/v1",
        # No models listed on purpose. One key here reaches every lab
        # OpenRouter resells, and that list is theirs to change, not ours to
        # copy - `openrouter.py` fetches it and maps it onto `Model`.
        dynamic=True,
    ),
}

# What each reasoning level means, in the terms that matter here rather than
# the provider's. Shown beside the selector so the choice is informed.
EFFORT_LABEL = {
    "none": "Off - fastest and cheapest, and noticeably worse at reading a "
            "drawing.",
    "low": "Brief - fine for a clean spec sheet where the numbers are printed.",
    "medium": "Moderate - a middle setting some providers offer; between "
              "brief and thorough.",
    "high": "Thorough - the default, and the right choice for a photograph.",
    "max": "Exhaustive - slowest and dearest; for a drawing you cannot get a "
           "clean reading from.",
}

# Two or three words for the segmented control, where there is no room for
# the sentence above.
EFFORT_SHORT = {
    "none": "fastest",
    "low": "brief",
    "medium": "moderate",
    "high": "thorough",
    "max": "exhaustive",
}

DEFAULT_PROVIDER = "anthropic"


def get_provider(provider_id: str) -> Provider | None:
    return PROVIDERS.get(provider_id)


def resolve(provider_id: str, model_id: str) -> tuple[Provider, Model] | None:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return None
    model = provider.model(model_id)
    if model is None:
        return None
    return provider, model


def mask(secret: str) -> str:
    """A hint that identifies a key without revealing it.

    Enough for someone to confirm which key is installed, useless to anyone who
    sees a screenshot.
    """
    secret = (secret or "").strip()
    if len(secret) <= 10:
        return "•" * len(secret)
    return f"{secret[:6]}…{secret[-4:]}"


def estimate_cost(model: Model, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * model.price_in
        + output_tokens / 1_000_000 * model.price_out
    )
