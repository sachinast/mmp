"""The event catalogue: the names the platform understands, and what each means.

Two kinds of event live here.

**Standard events** are the vocabulary every integration starts from. Some are
*owned by the SDK* — ``install``, ``session_start``, ``login`` — in that the SDK
sends them itself and refuses them from ``track()``; the rest are names the
platform recognises and reports on with defined semantics, such as ``purchase``
carrying revenue. Naming them here, with the properties each expects, is what
turns "send us events" into an integration someone can actually finish: the
dashboard shows this list, the SDK setup page generates constants from it, and
the reports group by it.

**Custom events** are whatever the advertiser's own product does — ``mining_started``,
``withdrawal_requested`` — and are stored per app in ``event_definitions``. An
event does not have to be defined to be accepted; definitions are documentation
and policy (a definition can *block* a name), never a gate on ingest. A gate
would mean an SDK release that adds an event silently loses data until someone
remembers to add it to a list.

Blocking is the one policy a definition carries. A blocked event is dropped at
the tracker, before anything is stored — the same reasoning as consent: applied
after persistence it becomes a deletion problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mmp_ingest.schema import canonical_event_name

CATEGORIES = ("lifecycle", "account", "commerce", "engagement", "content", "gaming")


@dataclass(frozen=True)
class EventProperty:
    name: str
    type: str
    description: str


@dataclass(frozen=True)
class StandardEvent:
    name: str
    display_name: str
    category: str
    description: str
    properties: tuple[EventProperty, ...] = field(default_factory=tuple)
    # Carries ``revenue_minor`` and ``currency`` by definition.
    revenue: bool = False
    # Sent by the SDK itself; the app must not send it through track().
    sdk_owned: bool = False


def _p(name: str, type_: str, description: str) -> EventProperty:
    return EventProperty(name, type_, description)


STANDARD_EVENTS: tuple[StandardEvent, ...] = (
    # --- lifecycle, owned by the SDK -------------------------------------
    StandardEvent(
        "install",
        "Install",
        "lifecycle",
        "First launch after installation. Sent once by the SDK; this is the event "
        "attribution is decided on.",
        sdk_owned=True,
    ),
    StandardEvent(
        "app_open",
        "App open",
        "lifecycle",
        "The app came to the foreground. Sent by the SDK on every launch after the first.",
        sdk_owned=True,
    ),
    StandardEvent(
        "session_start",
        "Session start",
        "lifecycle",
        "A new session began. Sessions are decided server-side from the app's timeout, "
        "so two devices with different clocks produce comparable counts.",
        sdk_owned=True,
    ),
    StandardEvent(
        "session_end",
        "Session end",
        "lifecycle",
        "The session ended after the app's inactivity timeout.",
        sdk_owned=True,
    ),
    StandardEvent(
        "consent_update",
        "Consent update",
        "lifecycle",
        "The user's consent choices changed. Sent by the SDK from setConsent().",
        sdk_owned=True,
    ),
    # --- account --------------------------------------------------------------
    StandardEvent(
        "signup",
        "Sign up",
        "account",
        "An account was created. Sent by the SDK when setUserId() is first called.",
        (_p("method", "string", "How they signed up: email, google, apple…"),),
        sdk_owned=True,
    ),
    StandardEvent(
        "login",
        "Login",
        "account",
        "An existing user signed in. Sent by the SDK from setUserId().",
        (_p("method", "string", "How they signed in."),),
        sdk_owned=True,
    ),
    StandardEvent(
        "profile_update",
        "Profile update",
        "account",
        "The user changed something about their account.",
        (_p("field", "string", "What changed."),),
    ),
    # --- commerce -------------------------------------------------------------
    StandardEvent(
        "view_item",
        "View item",
        "commerce",
        "A product or item detail screen was viewed.",
        (
            _p("item_id", "string", "Your identifier for the item."),
            _p("item_name", "string", "Human-readable name."),
            _p("category", "string", "Catalogue category."),
            _p("price_minor", "integer", "Price in minor units (cents, paise)."),
            _p("currency", "string", "ISO 4217 code."),
        ),
    ),
    StandardEvent(
        "add_to_cart",
        "Add to cart",
        "commerce",
        "An item was added to the basket.",
        (
            _p("item_id", "string", "Your identifier for the item."),
            _p("quantity", "integer", "How many."),
            _p("price_minor", "integer", "Unit price in minor units."),
            _p("currency", "string", "ISO 4217 code."),
        ),
    ),
    StandardEvent(
        "remove_from_cart",
        "Remove from cart",
        "commerce",
        "An item was removed from the basket.",
        (_p("item_id", "string", "Your identifier for the item."),),
    ),
    StandardEvent(
        "begin_checkout",
        "Begin checkout",
        "commerce",
        "The user started the checkout flow.",
        (
            _p("value_minor", "integer", "Basket value in minor units."),
            _p("currency", "string", "ISO 4217 code."),
            _p("item_count", "integer", "Items in the basket."),
        ),
    ),
    StandardEvent(
        "add_payment_info",
        "Add payment info",
        "commerce",
        "A payment method was entered or selected.",
        (_p("payment_type", "string", "card, upi, wallet…"),),
    ),
    StandardEvent(
        "purchase",
        "Purchase",
        "commerce",
        "A completed purchase. Send revenue_minor and currency on the event itself — "
        "this is what revenue reports and postbacks read.",
        (
            _p("order_id", "string", "Your order identifier, for reconciliation."),
            _p("item_count", "integer", "Items in the order."),
            _p("coupon", "string", "Promotion code, if any."),
        ),
        revenue=True,
    ),
    StandardEvent(
        "refund",
        "Refund",
        "commerce",
        "A purchase was refunded. Send the refunded amount as revenue_minor; reports subtract it.",
        (_p("order_id", "string", "The order being refunded."),),
        revenue=True,
    ),
    StandardEvent(
        "subscription_start",
        "Subscription start",
        "commerce",
        "A subscription or trial began.",
        (
            _p("plan", "string", "Plan identifier."),
            _p("trial", "boolean", "Whether this is a free trial."),
        ),
        revenue=True,
    ),
    StandardEvent(
        "subscription_renew",
        "Subscription renewal",
        "commerce",
        "A subscription renewed. Usually sent server-to-server from your billing "
        "webhook rather than from the device.",
        (_p("plan", "string", "Plan identifier."),),
        revenue=True,
    ),
    StandardEvent(
        "subscription_cancel",
        "Subscription cancelled",
        "commerce",
        "The user cancelled a subscription.",
        (
            _p("plan", "string", "Plan identifier."),
            _p("reason", "string", "Why, if you ask."),
        ),
    ),
    # --- engagement -----------------------------------------------------------
    StandardEvent(
        "tutorial_begin",
        "Tutorial begin",
        "engagement",
        "The onboarding or tutorial started.",
    ),
    StandardEvent(
        "tutorial_complete",
        "Tutorial complete",
        "engagement",
        "The onboarding or tutorial finished. A strong early signal of a retained user.",
    ),
    StandardEvent(
        "search",
        "Search",
        "engagement",
        "A search was performed.",
        (
            _p("query", "string", "What was searched for."),
            _p("results", "integer", "How many results."),
        ),
    ),
    StandardEvent(
        "share",
        "Share",
        "engagement",
        "Content was shared out of the app.",
        (
            _p("content_type", "string", "What kind of thing."),
            _p("channel", "string", "Where it went: whatsapp, sms, copy_link…"),
        ),
    ),
    StandardEvent(
        "invite",
        "Invite",
        "engagement",
        "The user invited someone.",
        (_p("channel", "string", "How the invitation was sent."),),
    ),
    StandardEvent(
        "rate",
        "Rate",
        "engagement",
        "The user rated the app or an item.",
        (
            _p("score", "integer", "The rating given."),
            _p("max", "integer", "The scale's maximum."),
        ),
    ),
    StandardEvent(
        "notification_open",
        "Notification opened",
        "engagement",
        "A push notification was opened.",
        (_p("campaign", "string", "Your notification campaign name."),),
    ),
    StandardEvent(
        "deep_link_open",
        "Deep link opened",
        "engagement",
        "The app was opened through a deep link.",
        (_p("destination", "string", "Where it routed."),),
    ),
    # --- content --------------------------------------------------------------
    StandardEvent(
        "content_view",
        "Content view",
        "content",
        "A piece of content was viewed: article, video, listing.",
        (
            _p("content_id", "string", "Your identifier."),
            _p("content_type", "string", "What kind of content."),
        ),
    ),
    StandardEvent(
        "ad_impression",
        "Ad impression",
        "content",
        "An in-app ad was shown. Send revenue_minor for ad revenue.",
        (
            _p("ad_unit", "string", "Placement identifier."),
            _p("network", "string", "Which network served it."),
        ),
        revenue=True,
    ),
    StandardEvent(
        "ad_click",
        "Ad click",
        "content",
        "An in-app ad was tapped.",
        (_p("ad_unit", "string", "Placement identifier."),),
    ),
    # --- gaming ---------------------------------------------------------------
    StandardEvent(
        "level_start",
        "Level start",
        "gaming",
        "A level or stage began.",
        (_p("level", "integer", "Level number or name."),),
    ),
    StandardEvent(
        "level_complete",
        "Level complete",
        "gaming",
        "A level or stage was completed.",
        (
            _p("level", "integer", "Level number or name."),
            _p("score", "integer", "Score achieved."),
        ),
    ),
    StandardEvent(
        "achievement_unlocked",
        "Achievement unlocked",
        "gaming",
        "An achievement was earned.",
        (_p("achievement_id", "string", "Your identifier."),),
    ),
    StandardEvent(
        "spend_virtual_currency",
        "Spend virtual currency",
        "gaming",
        "In-game currency was spent.",
        (
            _p("currency_name", "string", "coins, gems…"),
            _p("amount", "integer", "How much."),
            _p("item_id", "string", "What it bought."),
        ),
    ),
    StandardEvent(
        "earn_virtual_currency",
        "Earn virtual currency",
        "gaming",
        "In-game currency was earned.",
        (
            _p("currency_name", "string", "coins, gems…"),
            _p("amount", "integer", "How much."),
        ),
    ),
)

_BY_CANONICAL = {canonical_event_name(event.name): event for event in STANDARD_EVENTS}


def standard_event(name: str) -> StandardEvent | None:
    """The standard event a name refers to, however it is capitalised or spaced.

    ``Add To Cart``, ``add-to-cart`` and ``add_to_cart`` are all the same event;
    matching on the folded form is what the SDK does for reserved names, and the
    catalogue should agree with it.
    """
    return _BY_CANONICAL.get(canonical_event_name(name))


def blocked_events_cache_key(app_id: str) -> str:
    """Where the API publishes an app's blocked names for the tracker.

    One definition, imported by both services: the first time a cache key was
    typed out twice it was typed out differently.
    """
    return f"blocked_events:{app_id}"
