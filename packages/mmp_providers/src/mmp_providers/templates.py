"""Postback URL and body templates.

A postback rule contains a URL like::

    https://network.example/conv?click={{click_id}}&rev={{revenue}}

and the obvious way to render that is Jinja, which is already a dependency.
**It must not be.** Jinja is a programming language: given a template, it will
evaluate attribute access, call methods, and walk object graphs. Rendering a
customer-supplied template with it hands anyone who can create a postback rule
server-side template injection, and from there — through the usual chain of
``__class__``, ``__subclasses__`` and friends — code execution on a worker that
holds database credentials.

So this is not a template engine. It is a fixed allowlist of variable names
substituted from a dict, with every value URL-encoded on the way in. There is no
expression evaluation, no attribute access, no filters, and no way to add any:
a name that is not in ``ALLOWED_VARIABLES`` is left untouched or rejected, and
the substitution is a regex over a closed set.

The narrowness is the feature. A postback template can only ever produce a URL
built from values we chose to expose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

# The complete set of values a postback may reference. Adding to this list is a
# deliberate decision about what we are willing to send to a third party — every
# entry here leaves our systems, to an endpoint a customer configured.
ALLOWED_VARIABLES: frozenset[str] = frozenset(
    {
        "event_id",
        "event_name",
        "event_timestamp",
        "app_id",
        "user_id",
        "anonymous_id",
        "click_id",
        "campaign_id",
        "campaign_name",
        "source",
        "medium",
        "revenue",
        "currency",
        "platform",
        "country",
        "attribution_method",
        "install_timestamp",
        "sub1",
        "sub2",
        "sub3",
    }
)

# Values a partner put on its own tracking link — its click id, usually, in sub1.
# Allowed because returning them is the whole point: a partner matches an install
# to its click by the id it sent. Allowed only in a rule scoped to one campaign,
# because they belong to whichever partner sent that click, and an app-wide rule
# fires for every partner's installs. The API refuses the unscoped case.
SUB_PARAMETERS: frozenset[str] = frozenset({"sub1", "sub2", "sub3"})

# Deliberately strict: a name, nothing else. No dots, no brackets, no pipes, no
# whitespace — the syntax of every template-injection payload starts with one of
# those, and there is nothing here for them to reach even if they got through.
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]{0,40})\s*\}\}")

# Anything that *looks* like a placeholder, however malformed. Used to reject
# templates rather than to substitute them.
#
# The strict pattern above simply does not match `{{ config.items() }}`, so
# without this the expression would be left in the URL as literal text and sent
# to the partner verbatim. Not code execution — but a customer who typoed a
# variable would learn about it from their ad network weeks later, and a
# template-injection attempt would be silently accepted rather than reported.
# Both are better as an error at save time.
LOOSE_PLACEHOLDER = re.compile(r"\{\{.*?\}\}", re.DOTALL)

MAX_TEMPLATE_LENGTH = 4096
MAX_VALUE_LENGTH = 512


class TemplateError(ValueError):
    """The template references something it may not, or is malformed."""


@dataclass(frozen=True)
class TemplateCheck:
    variables: frozenset[str]
    unknown: frozenset[str]
    malformed: frozenset[str] = frozenset()

    @property
    def ok(self) -> bool:
        return not self.unknown and not self.malformed


def inspect(template: str) -> TemplateCheck:
    """Find the variables a template uses, and which of them are not allowed.

    Called when a rule is saved so an unknown variable is a validation error the
    user sees at once, rather than an empty query parameter a partner notices
    weeks later.
    """
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise TemplateError(f"template exceeds {MAX_TEMPLATE_LENGTH} characters")

    found = frozenset(match.group(1) for match in PLACEHOLDER.finditer(template))
    # Every `{{...}}` must be a well-formed, allowlisted placeholder. Anything
    # else — an expression, a typo, an injection attempt — is malformed.
    malformed = frozenset(
        match.group(0)
        for match in LOOSE_PLACEHOLDER.finditer(template)
        if not PLACEHOLDER.fullmatch(match.group(0))
    )
    return TemplateCheck(variables=found, unknown=found - ALLOWED_VARIABLES, malformed=malformed)


def uses_sub_parameters(*templates: str | None) -> frozenset[str]:
    """Which of sub1, sub2 and sub3 any of these templates reference."""
    used: set[str] = set()
    for template in templates:
        if template:
            used |= inspect(template).variables & SUB_PARAMETERS
    return frozenset(used)


def validate(template: str) -> None:
    check = inspect(template)
    if check.malformed:
        raise TemplateError("not valid placeholders: " + ", ".join(sorted(check.malformed)))
    if check.unknown:
        raise TemplateError("unknown template variables: " + ", ".join(sorted(check.unknown)))


def render(template: str, values: dict[str, object], *, encode: bool = True) -> str:
    """Substitute allowlisted variables. Nothing else happens.

    ``encode`` is true for URLs, where every value must be percent-encoded — an
    unencoded campaign name containing ``&`` would otherwise inject a parameter
    into the partner's request, which is a smaller version of the same class of
    bug this module exists to prevent. It is false for JSON bodies, where the
    JSON encoder does the escaping instead.
    """
    validate(template)

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values.get(name)
        if value is None:
            # An empty parameter rather than the literal "None", which some
            # partners would store as a string and report back to the customer.
            return ""
        text = str(value)[:MAX_VALUE_LENGTH]
        return quote(text, safe="") if encode else text

    return PLACEHOLDER.sub(substitute, template)


def variables_from(context: dict[str, object]) -> dict[str, object]:
    """Narrow an internal context down to what a postback may see.

    A whitelist on the way out as well as on the way in: even if a future caller
    passes the whole event, only the allowed keys can reach a third party.
    """
    return {key: value for key, value in context.items() if key in ALLOWED_VARIABLES}
