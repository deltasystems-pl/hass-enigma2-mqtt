"""The options flow's "Receiver menu": the user builds the menu the receiver shows.

A menu step, not a form with sections. The menu is a list of groups and rows whose length
the user decides, and a form's sections are a fixed set of fields: eight groups of
twenty-four rows would be a form of hundreds of fields, nearly all of them empty. A menu
step that offers add, edit and remove, each a short form of its own, is how Home Assistant's
own helpers edit a list, and every form in it uses the native selectors - the entity picker
for what a row is bound to, a list of the plugin's icons, and the controls the chosen entity
can be. A row stores the entity's registry id, so a renamed entity stays in the menu.

The edits are made to a working copy and saved only by "Save the menu", which stores the
whole menu in the entry's options and reloads the entry; the reload publishes it. Closing
the dialog without saving changes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import valid_entity_id
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.helpers.translation import async_get_translations
import voluptuous as vol

from .const import CAPABILITY_MENU, CONF_MENU, DOMAIN, MENU_ICON_NEUTRAL, MENU_ICONS
from .menu import (
    GROUP_ROWS,
    ITEM_ICON,
    ITEM_ID,
    ITEM_LABEL,
    KIND,
    KIND_GROUP,
    KIND_ROW,
    MAX_ENTRIES,
    MAX_GROUPS,
    MAX_LABEL,
    ROW_CONTROL,
    ROW_ENTITY,
    allowed_controls,
    find_row,
    menu_ids,
    menu_rows,
    new_item_id,
    resolve_entity,
    row_label,
    stored_binding,
    stored_menu,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .box import Enigma2Box

# The form fields that are not stored as they are.
FIELD_PLACE = "place"
FIELD_GROUP = "group"
FIELD_ROW = "row"
FIELD_REMOVE = "remove"
FIELD_ITEM = "item"
FIELD_DIRECTION = "direction"
DIRECTIONS = ("up", "down")
# The place of a row that is in no group.
PLACE_TOP = "top"


def menu_offered(box: Enigma2Box | None, options: Any) -> bool:
    """Whether the options offer the menu: the receiver can show one, or one is stored.

    A stored menu is always offered, so that it can be emptied after a downgrade.
    """
    return (box is not None and CAPABILITY_MENU in box.capabilities) or bool(
        stored_menu(options)
    )


class ReceiverMenuSteps:
    """The receiver menu's steps, mixed into the options flow."""

    if TYPE_CHECKING:
        hass: HomeAssistant
        config_entry: ConfigEntry

        def async_show_menu(self, **kwargs: Any) -> ConfigFlowResult: ...
        def async_show_form(self, **kwargs: Any) -> ConfigFlowResult: ...
        def async_create_entry(self, **kwargs: Any) -> ConfigFlowResult: ...
        def add_suggested_values_to_schema(
            self, data_schema: vol.Schema, suggested_values: Any
        ) -> vol.Schema: ...

    _menu_items: list[dict[str, Any]] | None = None
    # The id of the group or row being edited; None while one is being added.
    _menu_editing: str | None = None
    # A row between its form and the choice of its control.
    _menu_draft: dict[str, Any] | None = None
    _menu_place: str = PLACE_TOP

    # ----------------------------------------------------------------- helpers

    def _menu(self) -> list[dict[str, Any]]:
        """The working copy, read from the options the first time it is asked for."""
        if self._menu_items is None:
            self._menu_items = stored_menu(self.config_entry.options)
        return self._menu_items

    def _groups(self) -> list[dict[str, Any]]:
        return [item for item in self._menu() if item[KIND] == KIND_GROUP]

    def _group(self, group_id: str | None) -> dict[str, Any] | None:
        return next((group for group in self._groups() if group[ITEM_ID] == group_id), None)

    def _container(self, place: str) -> list[dict[str, Any]] | None:
        """The list a row in `place` belongs to: the top list or a group's rows."""
        if place == PLACE_TOP:
            return self._menu()
        group = self._group(place)
        return group[GROUP_ROWS] if group is not None else None

    def _place_of(self, row_id: str) -> str | None:
        """Where a stored row is: `top`, a group's id, or None for nowhere."""
        if any(item[ITEM_ID] == row_id for item in self._menu()):
            return PLACE_TOP
        for group in self._groups():
            if any(row[ITEM_ID] == row_id for row in group[GROUP_ROWS]):
                return group[ITEM_ID]
        return None

    def _can_add_group(self) -> bool:
        return len(self._groups()) < MAX_GROUPS and len(self._menu()) < MAX_ENTRIES

    def _can_add_row(self) -> bool:
        return len(self._menu()) < MAX_ENTRIES or any(
            len(group[GROUP_ROWS]) < MAX_ENTRIES for group in self._groups()
        )

    async def _menu_texts(self) -> dict[str, str]:
        """This integration's own words for the forms, in the user's language."""
        language = self.hass.config.language
        texts = await async_get_translations(self.hass, language, "common", {DOMAIN})
        texts.update(await async_get_translations(self.hass, language, "selector", {DOMAIN}))
        return texts

    @staticmethod
    def _text(texts: dict[str, str], key: str, fallback: str) -> str:
        return texts.get(f"component.{DOMAIN}.{key}") or fallback

    def _row_line(self, row: dict[str, Any], texts: dict[str, str]) -> str:
        control = self._text(
            texts, f"selector.menu_control.options.{row[ROW_CONTROL]}", row[ROW_CONTROL]
        )
        entity_id = resolve_entity(self.hass, row[ROW_ENTITY]) or row[ROW_ENTITY]
        return f"{row_label(self.hass, row)} - {entity_id} ({control})"

    def _outline(self, texts: dict[str, str]) -> str:
        """The menu as a list, for the menu step's description."""
        lines: list[str] = []
        for item in self._menu():
            if item[KIND] == KIND_GROUP:
                rows = item[GROUP_ROWS]
                lines.append(f"- **{item[ITEM_LABEL]}** ({len(rows)}/{MAX_ENTRIES})")
                lines.extend(f"  - {self._row_line(row, texts)}" for row in rows)
            else:
                lines.append(f"- {self._row_line(item, texts)}")
        return "\n".join(lines) or self._text(texts, "common.menu_empty", "-")

    def _icon_selector(self) -> selector.SelectSelector:
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(MENU_ICONS),
                translation_key="menu_icon",
                mode=selector.SelectSelectorMode.DROPDOWN,
                sort=False,
            )
        )

    def _resolve_entity(self, value: Any) -> str | None:
        """An entity id from the picker's answer, which may be a registry id."""
        if not isinstance(value, str):
            return None
        if valid_entity_id(value):
            return value
        return er.async_resolve_entity_id(er.async_get(self.hass), value)

    # ------------------------------------------------------------------- steps

    async def async_step_receiver_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The menu as it stands, and what can be done to it."""
        self._menu_editing = None
        self._menu_draft = None
        options: list[str] = []
        if self._can_add_group():
            options.append("menu_add_group")
        if self._can_add_row():
            options.append("menu_add_row")
        if self._groups():
            options.append("menu_edit_group")
        if menu_rows(self._menu()):
            options.append("menu_edit_row")
        if self._movable():
            options.append("menu_move")
        if self._menu():
            options.append("menu_remove")
        options.append("menu_save")
        return self.async_show_menu(
            step_id="receiver_menu",
            menu_options=options,
            description_placeholders={
                "menu": self._outline(await self._menu_texts()),
                "groups": str(MAX_GROUPS),
                "entries": str(MAX_ENTRIES),
            },
        )

    async def async_step_menu_add_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a new group."""
        self._menu_editing = None
        return await self.async_step_menu_group()

    async def async_step_menu_edit_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the group to change."""
        groups = self._groups()
        if user_input is not None and self._group(user_input.get(FIELD_GROUP)) is not None:
            self._menu_editing = user_input[FIELD_GROUP]
            return await self.async_step_menu_group()
        if not groups:
            return await self.async_step_receiver_menu()
        return self.async_show_form(
            step_id="menu_edit_group",
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_GROUP): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=group[ITEM_ID], label=group[ITEM_LABEL]
                                )
                                for group in groups
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_menu_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A group's name and icon; a new group goes to the end of the top list."""
        group = self._group(self._menu_editing)
        errors: dict[str, str] = {}
        if user_input is not None:
            label = " ".join(str(user_input.get(ITEM_LABEL) or "").split())
            icon = user_input.get(ITEM_ICON)
            icon = icon if icon in MENU_ICONS else MENU_ICON_NEUTRAL
            if not label:
                errors[ITEM_LABEL] = "menu_label_required"
            elif group is None and not self._can_add_group():
                errors["base"] = "menu_full"
            else:
                if group is None:
                    self._menu().append(
                        {
                            KIND: KIND_GROUP,
                            ITEM_ID: new_item_id(KIND_GROUP, menu_ids(self._menu())),
                            ITEM_LABEL: label[:MAX_LABEL],
                            ITEM_ICON: icon,
                            GROUP_ROWS: [],
                        }
                    )
                else:
                    group[ITEM_LABEL] = label[:MAX_LABEL]
                    group[ITEM_ICON] = icon
                return await self.async_step_receiver_menu()
        schema = vol.Schema(
            {
                vol.Required(ITEM_LABEL): selector.TextSelector(),
                vol.Required(ITEM_ICON, default=MENU_ICON_NEUTRAL): self._icon_selector(),
            }
        )
        suggested = user_input or (
            {ITEM_LABEL: group[ITEM_LABEL], ITEM_ICON: group[ITEM_ICON]} if group else {}
        )
        return self.async_show_form(
            step_id="menu_group",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"label_max": str(MAX_LABEL)},
        )

    async def async_step_menu_add_row(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a new row."""
        self._menu_editing = None
        return await self.async_step_menu_row()

    async def async_step_menu_edit_row(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the row to change."""
        rows = menu_rows(self._menu())
        if user_input is not None and find_row(self._menu(), user_input.get(FIELD_ROW)):
            self._menu_editing = user_input[FIELD_ROW]
            return await self.async_step_menu_row()
        if not rows:
            return await self.async_step_receiver_menu()
        return self.async_show_form(
            step_id="menu_edit_row",
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_ROW): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._choices(groups=False),
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    def _choices(self, *, groups: bool) -> list[selector.SelectOptionDict]:
        """Every row - and every group, when asked - as an option, named by where it is."""
        choices: list[selector.SelectOptionDict] = []
        for item in self._menu():
            if item[KIND] == KIND_GROUP:
                if groups:
                    choices.append(
                        selector.SelectOptionDict(value=item[ITEM_ID], label=item[ITEM_LABEL])
                    )
                choices.extend(
                    selector.SelectOptionDict(
                        value=row[ITEM_ID],
                        label=f"{item[ITEM_LABEL]} / {row_label(self.hass, row)}",
                    )
                    for row in item[GROUP_ROWS]
                )
            else:
                choices.append(
                    selector.SelectOptionDict(
                        value=item[ITEM_ID], label=row_label(self.hass, item)
                    )
                )
        return choices

    async def async_step_menu_row(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What a row is bound to, where it goes, and what it is called."""
        existing = (
            find_row(self._menu(), self._menu_editing) if self._menu_editing else None
        )
        current_place = self._place_of(existing[ITEM_ID]) if existing else None
        texts = await self._menu_texts()
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = self._resolve_entity(user_input.get(ROW_ENTITY))
            place = user_input.get(FIELD_PLACE) or PLACE_TOP
            container = self._container(place)
            if entity_id is None:
                errors[ROW_ENTITY] = "menu_entity_not_supported"
            elif container is None or (
                place != current_place and len(container) >= MAX_ENTRIES
            ):
                errors[FIELD_PLACE] = "menu_place_full"
            else:
                draft: dict[str, Any] = {
                    KIND: KIND_ROW,
                    ROW_ENTITY: stored_binding(self.hass, entity_id),
                }
                label = " ".join(str(user_input.get(ITEM_LABEL) or "").split())
                if label:
                    draft[ITEM_LABEL] = label[:MAX_LABEL]
                if user_input.get(ITEM_ICON) in MENU_ICONS:
                    draft[ITEM_ICON] = user_input[ITEM_ICON]
                self._menu_draft = draft
                self._menu_place = place
                controls = allowed_controls(self.hass, entity_id)
                if len(controls) > 1:
                    return await self.async_step_menu_row_control()
                return await self._async_commit_row(controls[0])

        places = [
            selector.SelectOptionDict(
                value=PLACE_TOP, label=self._text(texts, "common.menu_top_level", PLACE_TOP)
            ),
            *(
                selector.SelectOptionDict(value=group[ITEM_ID], label=group[ITEM_LABEL])
                for group in self._groups()
            ),
        ]
        schema = vol.Schema(
            {
                vol.Required(ROW_ENTITY): selector.EntitySelector(),
                vol.Required(FIELD_PLACE, default=PLACE_TOP): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=places, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(ITEM_LABEL): selector.TextSelector(),
                vol.Optional(ITEM_ICON): self._icon_selector(),
            }
        )
        suggested: dict[str, Any] = dict(user_input or {})
        if user_input is None and existing is not None:
            suggested = {
                ROW_ENTITY: resolve_entity(self.hass, existing[ROW_ENTITY])
                or existing[ROW_ENTITY],
                FIELD_PLACE: current_place or PLACE_TOP,
                **{key: existing[key] for key in (ITEM_LABEL, ITEM_ICON) if key in existing},
            }
        return self.async_show_form(
            step_id="menu_row",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"label_max": str(MAX_LABEL)},
        )

    async def async_step_menu_row_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """How the row shows its entity, where the entity can be shown more than one way."""
        draft = self._menu_draft
        if draft is None:
            return await self.async_step_receiver_menu()
        entity_id = resolve_entity(self.hass, draft[ROW_ENTITY]) or ""
        controls = allowed_controls(self.hass, entity_id)
        if user_input is not None and user_input.get(ROW_CONTROL) in controls:
            return await self._async_commit_row(user_input[ROW_CONTROL])
        existing = (
            find_row(self._menu(), self._menu_editing) if self._menu_editing else None
        )
        default = (
            existing[ROW_CONTROL]
            if existing is not None and existing[ROW_CONTROL] in controls
            else controls[0]
        )
        return self.async_show_form(
            step_id="menu_row_control",
            data_schema=vol.Schema(
                {
                    vol.Required(ROW_CONTROL, default=default): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=controls,
                            translation_key="menu_control",
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"entity": row_label(self.hass, draft)},
        )

    async def _async_commit_row(self, control: str) -> ConfigFlowResult:
        """Store the row in the working copy and go back to the menu.

        A row keeps its id while it stays bound to the same entity as the same control; a
        new binding is a new row as far as the receiver can tell, and gets a new id.
        """
        draft = self._menu_draft
        assert draft is not None
        menu = self._menu()
        existing = find_row(menu, self._menu_editing) if self._menu_editing else None
        row = {**draft, ROW_CONTROL: control}
        # The same entity whichever form either is stored in: a row stored by its entity id
        # before the entity had a registry entry is still the same binding.
        if (
            existing is not None
            and resolve_entity(self.hass, existing[ROW_ENTITY])
            == resolve_entity(self.hass, row[ROW_ENTITY])
            and existing[ROW_CONTROL] == control
        ):
            row[ITEM_ID] = existing[ITEM_ID]
        else:
            row[ITEM_ID] = new_item_id(KIND_ROW, menu_ids(menu))
        row = {KIND: KIND_ROW, ITEM_ID: row.pop(ITEM_ID), **row}
        target = self._container(self._menu_place)
        assert target is not None
        old_place = self._place_of(existing[ITEM_ID]) if existing is not None else None
        if existing is not None and old_place == self._menu_place:
            index = next(i for i, item in enumerate(target) if item[ITEM_ID] == existing[ITEM_ID])
            target[index] = row
        else:
            if existing is not None and old_place is not None:
                source = self._container(old_place)
                assert source is not None
                source[:] = [item for item in source if item[ITEM_ID] != existing[ITEM_ID]]
            target.append(row)
        return await self.async_step_receiver_menu()

    async def async_step_menu_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove groups and rows; a group goes with every row in it."""
        if user_input is not None:
            chosen = set(user_input.get(FIELD_REMOVE) or [])
            menu = self._menu()
            menu[:] = [item for item in menu if item[ITEM_ID] not in chosen]
            for group in self._groups():
                group[GROUP_ROWS] = [
                    row for row in group[GROUP_ROWS] if row[ITEM_ID] not in chosen
                ]
            return await self.async_step_receiver_menu()
        return self.async_show_form(
            step_id="menu_remove",
            data_schema=vol.Schema(
                {
                    vol.Optional(FIELD_REMOVE, default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._choices(groups=True),
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    def _lists(self) -> list[list[dict[str, Any]]]:
        """Every list the menu has: the top list and each group's rows."""
        return [self._menu(), *(group[GROUP_ROWS] for group in self._groups())]

    def _movable(self) -> bool:
        return any(len(entries) > 1 for entries in self._lists())

    def _move(self, item_id: str, direction: str) -> None:
        """Swap an entry with its neighbour in its own list; at an end it stays."""
        for entries in self._lists():
            index = next(
                (i for i, entry in enumerate(entries) if entry[ITEM_ID] == item_id), None
            )
            if index is None:
                continue
            other = index - 1 if direction == "up" else index + 1
            if 0 <= other < len(entries):
                entries[index], entries[other] = entries[other], entries[index]
            return

    async def async_step_menu_move(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move a group or a control one place up or down within its own list."""
        if user_input is not None:
            if user_input.get(FIELD_DIRECTION) in DIRECTIONS:
                self._move(str(user_input.get(FIELD_ITEM)), user_input[FIELD_DIRECTION])
            return await self.async_step_receiver_menu()
        if not self._movable():
            return await self.async_step_receiver_menu()
        return self.async_show_form(
            step_id="menu_move",
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_ITEM): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._choices(groups=True),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(FIELD_DIRECTION, default="up"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(DIRECTIONS),
                            translation_key="menu_direction",
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    async def async_step_menu_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the menu with the other options; the reload that follows publishes it."""
        options = dict(self.config_entry.options)
        if menu := self._menu():
            options[CONF_MENU] = menu
        else:
            options.pop(CONF_MENU, None)
        return self.async_create_entry(data=options)
