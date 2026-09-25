# ADR-0007: Entity ids follow the installation's language, so every translated name is load-bearing

**Status:** accepted
**Date:** 2026-09-25
**Supersedes:** [ADR-0000](0000-prd.md) §6.3, in part - its last paragraph, which says entity
ids derive from the English translation keys and that "display names translate, ids do not".
Everything else in §6.3 stands.

## Context

The PRD assumed that an entity id comes from the entity's English translation key, so that the
Polish and German names in `translations/*.json` would be free to change: a word could be
improved without touching anything a household had built on the entity.

That is not how Home Assistant names an entity that has a translated name. When the entity is
first registered, Home Assistant builds its object id from the name as it is displayed in the
installation's configured language. The channel sensor is `sensor.dekoder_salon_kanal` on a
Polish installation and `sensor.dekoder_salon_channel` on an English one; the translation key,
`channel`, is the same on both. A receiver running on a Polish installation shows the same
pattern for every entity it has: the "last error", "next timer" and "refresh discovery" entities
have Polish ids, not English ones.

The test suite did not show this, because it runs Home Assistant in English, where the displayed
name and the English key give the same id. A test that renames an English name and sees the id
change is consistent with both readings.

## Decision

**The unique id is `<node_id>_<key>`**, where the key is the translation key; it is the same in
every language and it is what the registry keys the entity by. **The entity id is not language
independent**: it is made once, from the displayed name in the installation's language, and the
registry keeps it from then on - also when the language changes later.

**Every translation's name is therefore load-bearing, in every language.** A name that has
shipped is not renamed in `strings.json`, `en.json`, `pl.json` or `de.json`. A new entity's name
is chosen with the same care in each language, because the first release that carries it fixes
the id on every installation that registers it.

**Documentation does not promise an id.** Examples use one language and say so, and point the
reader to the device page for the ids of their own installation.

## Consequences

- A rename leaves existing installations with their registered ids, but gives every new
  installation, and any entity that is registered again - after the entry is removed and added
  back - a different id from the one existing automations, dashboards and shared examples use,
  and it changes the name the household sees.
- Two installations in different languages have different entity ids for the same receiver.
  Automations and dashboards shared between them need editing; the unique id is the only stable
  handle.
- A translation that is wrong or clumsy is corrected only when the correction is worth a new id
  on new installations, and the change is called out in the changelog.
- If Home Assistant ever builds object ids from the translation key instead, this record is
  superseded, and the names become free to change again.
