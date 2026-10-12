"""Which of Home Assistant's two error types a refused or failed call raises.

Every button, select, action and service of this integration answers a call that did not
do what was asked by raising, with a translated sentence. Home Assistant has two types for
that, and they say different things:

- `ServiceValidationError` - the call was refused and nothing was done, for a reason the
  caller can act on or wait out: what was asked for (a version that cannot be had, a
  downgrade), or the moment (a rate limit, standby, a recording, another install running,
  a zap history too short to clear). The frontend shows the sentence as it is, and the
  log gets one line without a traceback.
- `HomeAssistantError` - something was tried and failed, the receiver did not answer, or
  the reason is the receiver's own English sentence without a code, which can be either.
  The frontend puts "Failed to perform the action" in front of the sentence.

The second is the base class of the first, so code that catches `HomeAssistantError` sees
both. Where a refusal carries a reason code the code decides; where it carries none the
type stays `HomeAssistantError`, because a sentence cannot be classified.

What neither type changes is the REST API. `POST /api/services/<domain>/<service>` turns
only a schema error and an unknown service into a client error (Home Assistant 2026.9);
any `HomeAssistantError`, a `ServiceValidationError` included, leaves the request handler
unanswered, and the web server answers 500 and logs the traceback itself. A caller that
needs the sentence uses the WebSocket API, which returns it with its translation key under
`service_validation_error` or `home_assistant_error`.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN


def service_error(
    refused: bool, key: str, placeholders: dict[str, str] | None = None
) -> HomeAssistantError:
    """Return the error for a translated sentence: a refusal, or a failure.

    `refused` is the whole decision - see the module docstring for what counts.
    """
    kind = ServiceValidationError if refused else HomeAssistantError
    return kind(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )
