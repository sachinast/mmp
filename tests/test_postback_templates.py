"""Postback templates.

The obvious implementation is Jinja, which is already a dependency. It must not
be: Jinja is a programming language, and rendering a customer-supplied template
with it hands anyone who can create a postback rule server-side template
injection, and from there code execution on a worker holding database
credentials.
"""

from __future__ import annotations

import pytest
from mmp_providers.templates import (
    ALLOWED_VARIABLES,
    TemplateError,
    inspect,
    render,
    validate,
    variables_from,
)

CONTEXT = {
    "click_id": "018f-abc",
    "event_name": "purchase",
    "revenue": 1999,
    "currency": "USD",
    "campaign_name": "Summer & Winter",
    "user_id": None,
}


def test_substitution_works():
    rendered = render(
        "https://n.example/c?click={{click_id}}&rev={{revenue}}&cur={{currency}}", CONTEXT
    )
    assert rendered == "https://n.example/c?click=018f-abc&rev=1999&cur=USD"


def test_values_are_url_encoded():
    """An unencoded campaign name containing '&' would inject a parameter into
    the partner's request — a smaller version of the same class of bug this
    module exists to prevent."""
    rendered = render("https://n.example/c?name={{campaign_name}}", CONTEXT)
    assert "Summer%20%26%20Winter" in rendered
    assert "Summer & Winter" not in rendered


def test_missing_values_render_empty_not_none():
    """Some partners store the literal string 'None' and report it back to the
    customer as a user id."""
    rendered = render("https://n.example/c?u={{user_id}}", CONTEXT)
    assert rendered == "https://n.example/c?u="
    assert "None" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        "{{7*7}}",
        "{{ config }}",
        "{{ config.items() }}",
        "{{ self.__init__.__globals__ }}",
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ cycler.__init__.__globals__.os.popen('id').read() }}",
        "{{ request.application.__globals__ }}",
        "{{ lipsum.__globals__['os'].popen('id').read() }}",
        "{% for x in ().__class__.__base__.__subclasses__() %}{{x}}{% endfor %}",
        "{{ url_for.__globals__ }}",
        "{{ get_flashed_messages.__globals__ }}",
        "{{ ''.__class__.__base__.__subclasses__()[104].__init__.__globals__ }}",
    ],
)
def test_template_injection_payloads_are_rejected(payload):
    """The standard SSTI corpus. None of these may be accepted, and none may be
    silently passed through to a partner as literal text either."""
    with pytest.raises(TemplateError):
        validate(f"https://n.example/c?x={payload}")


def test_unknown_variables_are_rejected_at_save_time():
    """A typo must be a validation error the user sees now, not an empty
    parameter their ad network notices in a month."""
    with pytest.raises(TemplateError, match="unknown template variables"):
        validate("https://n.example/c?x={{clickid}}")


def test_malformed_placeholders_are_rejected():
    with pytest.raises(TemplateError, match="not valid placeholders"):
        validate("https://n.example/c?x={{ not a name }}")


def test_oversized_templates_are_rejected():
    with pytest.raises(TemplateError):
        validate("https://n.example/?x=" + "{{click_id}}" * 500)


def test_values_are_truncated():
    """A partner's URL length limit is not ours to discover in production."""
    rendered = render("{{click_id}}", {"click_id": "x" * 5000})
    assert len(rendered) <= 512


def test_inspect_reports_what_a_template_uses():
    check = inspect("https://n.example/?a={{click_id}}&b={{revenue}}")
    assert check.variables == {"click_id", "revenue"}
    assert check.ok


def test_the_allowlist_is_a_closed_set():
    """Every variable a postback can reference leaves our systems, to an
    endpoint a customer configured. Adding one is a deliberate decision."""
    assert "password" not in ALLOWED_VARIABLES
    assert "api_key" not in ALLOWED_VARIABLES
    assert "ip" not in ALLOWED_VARIABLES
    assert "ip_hash" not in ALLOWED_VARIABLES
    assert "device_hash" not in ALLOWED_VARIABLES
    assert "email" not in ALLOWED_VARIABLES


def test_context_is_narrowed_on_the_way_out():
    """A whitelist on the way out as well as in: even if a future caller passes
    the whole event, only allowed keys can reach a third party."""
    narrowed = variables_from(
        {"click_id": "abc", "ip_hash": b"secret", "password_hash": "x", "revenue": 1}
    )
    assert narrowed == {"click_id": "abc", "revenue": 1}


def test_jinja_is_not_used_for_templates():
    """A structural guard.

    The whole argument of this module is that a template engine must not touch
    customer input. If someone imports one here later, that argument is gone and
    nothing else would notice.
    """
    import mmp_providers.templates as module

    source = __import__("inspect").getsource(module)
    for engine in ("jinja", "Template(", "eval(", "exec("):
        assert engine not in source, f"{engine} must not appear in the template module"
