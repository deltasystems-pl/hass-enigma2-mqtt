# Roadmap

Where the integration stands and what comes next. What each release changed is in
[CHANGELOG.md](CHANGELOG.md); why, in the [ADRs](docs/adr/).

## Milestones

- [x] **M0** - product requirements approved ([ADR-0000](docs/adr/0000-prd.md),
      [ADR-0001](docs/adr/0001-m0-decisions.md)); the scope added since is
      [ADR-0002](docs/adr/0002-scope-after-m0.md)
- [x] **M1** - config flow (discovered + manual), the device page, diagnostics. Released as
      **v0.1.0**
- [x] **M2** - plugin state and discovery complete, commands with guards: *the receiver plugin's
      milestone, released as its **v0.2.0**. Its long passive soak and the
      deep-standby drill are still open*
- [x] **M3** - the entities, actions, device triggers, diagnostics, translations. Released
      as **v0.2.0**
- [x] **M4** - SSH installer, bundled IPK and `update`: *run end to end on a receiver, rollback
      included.* The installer was run on a box that did not have the plugin, and a rollback
      exercised for real - a deliberately wrong broker password, the plugin refused, the receiver
      restored to the byte and its interface restarted. The four defects those runs found are
      fixed in 0.2.0: a pending discovery offer blocked the guided install, the success
      screen was lost, the rollback misjudged the restart and left its lock behind, and a refusal
      over a mismatched identity named neither side. The run after those fixes closed it - the
      rollback's verdict was right, its transaction lock released and the receiver reported as
      restored, and a successful install ended on its own screen again. One box, one image: the
      others still need testers
- [ ] **M5** - public beta `v0.x`: releases, HACS custom repository, call for testers
- [ ] **M6** - `v1.0.0`: HACS default store, deep standby and Wake-on-LAN drilled
- [ ] **M7** - afterwards: broker-login provisioning, further images

## Where the releases fit

HACS serves **0.3.0** and the receiver plugin's feed serves its own **0.3.0**, so the two halves
are in step and the version on a receiver or in HACS says which release you are running.

0.2.0 (fixes) and 0.3.0 (features) came out of two days of household use, which produced a list
of problems and a list of wants; the plan is
[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md), and the remote uninstall
that 0.3.0 added is [ADR-0004](docs/adr/0004-remote-uninstall.md) and
[ADR-0006](docs/adr/0006-remote-uninstall-plugin-acts-ssh-verifies.md). Everything either release
shipped is listed in [CHANGELOG.md](CHANGELOG.md).

## Testers wanted

OpenATV, OpenPLi and OpenBH have no test box. If you have one, open an issue - there is no
per-image thread to find yet, so yours would start it. [CONTRIBUTING.md](CONTRIBUTING.md) says
what helps.
