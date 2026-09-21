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
| `entity-unique-id` — every entity has one (`<node_id>_<key>`) | [x] | M3 |
| `has-entity-name` — entities use `_attr_has_entity_name` | [x] | M3 |
| `entity-event-setup` — subscriptions set up and torn down with the entity | [x] | M3 |
| `common-modules` — `coordinator.py` / `entity.py` where they belong | [x] | M3 |
| `appropriate-polling` — not applicable, the integration is push-only | [x] | M0 |
| `action-setup` — actions registered once, validated by schema | [x] | M3 |
| `docs-actions` — every action documented | [x] | M3 |
| `docs-high-level-description` — what the integration is, in the README | [x] | M1 |
| `docs-installation-instructions` — HACS and manual | [x] | M1 |
| `docs-removal-instructions` — how to remove it cleanly, including `cmd/reset` | [x] | M1 |
| `brands` — icon and logo shipped in `brand/` | [x] | M1 |
| `dependency-transparency` — dependencies pinned and auditable | [x] | M4 bundles the GPL IPK with its exact corresponding source archive; metadata pins commit, sizes and SHA-256 values, and CI reproduces both instead of trusting an opaque download. |

## Silver

| Rule | Done | Milestone |
|---|---|---|
| `config-entry-unloading` — unload and reload without a restart | [x] | M1 |
| `entity-unavailable` — entities follow the box's availability topic | [x] | M3 |
| `log-when-unavailable` — logged once when it goes away, once when it returns | [x] | M3 |
| `action-exceptions` — actions raise `HomeAssistantError` / `ServiceValidationError` | [x] | M3 |
| `parallel-updates` — `PARALLEL_UPDATES = 0`, the integration is push-only | [x] | M3 |
| `reauthentication-flow` — re-enter the SSH password when it stops working | [x] | M4, covered by config-flow and update-action tests; live installer acceptance remains pending. |
| `docs-configuration-parameters` — every option explained | [x] | M3 |
| `docs-installation-parameters` — every setup field explained | [x] | M3 |
| `integration-owner` — a codeowner in the manifest | [x] | M1 |
| `test-coverage` — at least 95 % of the integration's lines | [x] | M4 |

## Beyond silver

Not targeted for v1, but recorded so the decision is visible. Four gold rules are met anyway,
because they were cheap: `diagnostics` and `devices` landed in M1 and the download grew to
every state topic in M3; `discovery` is inherent — the plugin announces itself over MQTT; and
`reconfiguration-flow` landed with the options in M3. `entity-translations` is met for the
three languages shipped. `dynamic-devices`, `stale-devices`, `repair-issues`, and the platinum
async/typing rules are M5 or later.

Two notes on what the ticks above do **not** claim.

`action-setup` reads literally as "register actions in `async_setup`". These are entity
actions, registered on the media player platform, which is where Home Assistant requires an
entity action to be registered; the rule's intent — a schema on every action, validated before
anything is published, and an action that exists whether or not a box is loaded — is met.

`test-coverage` is met as of the 449-test suite: 95.6% statement coverage (142 of 3212 lines
uncovered) on Python 3.14.4 against Home Assistant 2026.9.2, measured locally with
`pytest --cov`. The suite covers every platform, every action, the device triggers, both flows
and the guarded installer, including its refusals, its rollback and the races its durable lock
can lose. Nothing is excluded with `# pragma: no cover`, so the number is the whole file.

What closed the gap was the transport, which was the part a fake session could never check: the
real AsyncSSH adapter now runs against an SSH server started inside the test process — a
receiver that is not there, one that refuses the password, one whose host key has changed, one
that never answers a command — and the assertions are the error codes a user would end up
reading. The receiver-side helper is also exercised through its command line, which is the
interface the installer actually uses, rather than only through its functions.

The lowest files are `installer_helper.py` at 91% and `config_flow.py` at 92%. What remains
uncovered there is concentrated in branches that need a real receiver's filesystem or a real
failure of the SSH probe mid-flow. That is a gap in what can be simulated, and it is named here
rather than closed with a pragma.

The intermittent failure previously recorded against
`test_aborting_progress_cancels_the_transaction_and_clears_secrets` is fixed. Its cause was in
the test, not the flow: it registered a plain function as a bus listener, so Home Assistant
classified it as an executor job and ran it in a worker thread, where it set an
`asyncio.Event` off the loop and the waiter missed the wakeup. It failed 19 times in 40 runs
before the fix and 0 times in 60 after it.

None of this is evidence that receiver installation or rollback has passed live acceptance. It
has not.

## What CI enforces today

- **hassfest** — manifest, dependencies and translations are structurally valid.
- **HACS action** — the repository meets the store's requirements.
- **ruff** — lint and import order (`E`, `F`, `W`, `I`, `B`, `UP`).
- **pytest** — the test suite, against the Home Assistant release pinned by
  `pytest-homeassistant-custom-component`.
- **Reproduce bundled receiver plugin** — the committed IPK and source archive are rebuilt from
  the pinned plugin commit and compared byte for byte.

All five run on every push to `main`, on every pull request, once a week — and **in front of
every release**. The release workflow calls the same workflow and publishes nothing unless all of
it passes. That is not redundant with the branch ruleset: a tag is pushed by a person, at
whatever commit they choose, so "main was green" is not evidence about the tree being released.

Two consequences of putting those jobs in front of the publisher are worth stating. Every
release is now gated on a **live checkout of `deltasystems-pl/enigma2-mqtt-bridge` at the commit
the bundle pins**, rebuilt and compared byte for byte — so a release cannot be cut while that
repository is unreachable, and it cannot be cut at all if the committed IPK no longer matches
the source it claims. And the third-party actions that now sit in the publishing path
(`hassfest`, the HACS action, `action-gh-release`) are **pinned by commit SHA** with the version
in a comment beside them, because a moving `@master`, `@main` or `@v2` is somebody else's push
running with this repository's release token.
