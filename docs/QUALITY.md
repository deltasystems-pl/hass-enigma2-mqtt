# Quality bar

Home Assistant's [integration quality scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is a badge only core integrations can carry. A custom integration cannot claim it — but the
rules are still the best available checklist, so this repository holds itself to the
**silver** tier and tracks it here.

Every box is unticked until the behaviour is real and covered by a test or by a documented
by-effect check. The milestone column says when it is due.

## Bronze

| Rule | Done | Milestone |
|---|---|---|
| `config-flow` — set up from the UI, no YAML | [x] | M1 |
| `config-flow-test-coverage` — the flow is fully tested | [x] | M1 |
| `unique-config-entry` — a box cannot be added twice (`node_id`) | [x] | M1 |
| `test-before-configure` — the flow fails early when the broker or the box is wrong | [x] | M1 |
| `test-before-setup` — setup raises `ConfigEntryNotReady` instead of half-working | [x] | M1 |
| `runtime-data` — entry state in `entry.runtime_data`, typed | [x] | M1 |
| `entity-unique-id` — every entity has one (`<node_id>_<key>`) | [ ] | M3 |
| `has-entity-name` — entities use `_attr_has_entity_name` | [ ] | M3 |
| `entity-event-setup` — subscriptions set up and torn down with the entity | [ ] | M3 |
| `common-modules` — `coordinator.py` / `entity.py` where they belong | [ ] | M3 |
| `appropriate-polling` — not applicable, the integration is push-only | [x] | M0 |
| `action-setup` — actions registered in `async_setup`, validated | [ ] | M3 |
| `docs-actions` — every action documented | [ ] | M3 |
| `docs-high-level-description` — what the integration is, in the README | [x] | M1 |
| `docs-installation-instructions` — HACS and manual | [x] | M1 |
| `docs-removal-instructions` — how to remove it cleanly, including `cmd/reset` | [ ] | M3 |
| `brands` — icon and logo shipped in `brand/` | [x] | M1 |
| `dependency-transparency` — dependencies pinned, built from source, no blobs | [x] | M1 |

## Silver

| Rule | Done | Milestone |
|---|---|---|
| `config-entry-unloading` — unload and reload without a restart | [x] | M1 |
| `entity-unavailable` — entities follow the box's availability topic | [ ] | M3 |
| `log-when-unavailable` — logged once when it goes away, once when it returns | [ ] | M3 |
| `action-exceptions` — actions raise `HomeAssistantError` / `ServiceValidationError` | [ ] | M3 |
| `parallel-updates` — `PARALLEL_UPDATES = 0`, the integration is push-only | [ ] | M3 |
| `reauthentication-flow` — re-enter the SSH password when it stops working | [ ] | M4 |
| `docs-configuration-parameters` — every option explained | [ ] | M3 |
| `docs-installation-parameters` — every setup field explained | [ ] | M3 |
| `integration-owner` — a codeowner in the manifest | [x] | M1 |
| `test-coverage` — at least 95 % of the integration's lines | [ ] | M4 |

## Beyond silver

Not targeted for v1, but recorded so the decision is visible: `diagnostics` and
`devices` (gold) landed in M1 anyway because they are cheap and useful — the diagnostics
download grows with the entities in M3;
`discovery` (gold) is inherent — the plugin announces itself over MQTT; `reconfiguration-flow`
(gold) lands with the options in M3. `dynamic-devices`, `stale-devices`, `repair-issues`,
`entity-translations` beyond en/pl/de, and the platinum async/typing rules are M5 or later.

## What CI enforces today

- **hassfest** — manifest, dependencies and translations are structurally valid.
- **HACS action** — the repository meets the store's requirements.
- **ruff** — lint and import order (`E`, `F`, `W`, `I`, `B`, `UP`).
- **pytest** — the test suite, against the Home Assistant release pinned by
  `pytest-homeassistant-custom-component`.
