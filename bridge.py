"""Bridge between DiscordEmbeder (Components V2 builder) and the playground.

A "design" is the canonical Components V2 payload shape the builder exports
(the `tree` array of a .discordv2proj.json file — top-level components, Section
carrying `components` + `accessory`, Container/ActionRow nesting `components`).

`design_to_code` is a faithful port of the builder's discordPyExporter
(src/exporters/discordpy.ts): same formatting, same kwargs, byte-comparable
output for the same payload.
"""

from __future__ import annotations

import json

from project_state import validate_project

BUTTON_STYLES = {1: "primary", 2: "secondary", 3: "success", 4: "danger", 5: "link"}

SELECT_CLASSES = {3: "Select", 5: "UserSelect", 6: "RoleSelect", 7: "MentionableSelect", 8: "ChannelSelect"}

CHANNEL_TYPES = {
    0: "text", 1: "private", 2: "voice", 3: "group", 4: "category", 5: "news",
    10: "news_thread", 11: "public_thread", 12: "private_thread", 13: "stage_voice",
    15: "forum", 16: "media",
}

SEPARATOR_SPACING = {1: "small", 2: "large"}

# Component type ids (the schema is the contract — do not renumber)
ACTION_ROW, BUTTON = 1, 2
SECTION, TEXT_DISPLAY, THUMBNAIL, MEDIA_GALLERY, FILE, SEPARATOR, CONTAINER = 9, 10, 11, 12, 13, 14, 17


def _ind(depth: int) -> str:
    return "    " * depth


def _py_string(value: str) -> str:
    return json.dumps(value)


def _kv(fields: list[tuple[str, str]]) -> str:
    return ", ".join(f"{k}={v}" for k, v in fields)


def _emoji_arg(emoji: dict | None) -> str | None:
    if not emoji:
        return None
    if emoji.get("id"):
        prefix = "a" if emoji.get("animated") else ""
        return _py_string(f"<{prefix}:{emoji['name']}:{emoji['id']}>")
    if emoji.get("name"):
        return _py_string(emoji["name"])
    return None


def _py_button_args(data: dict) -> str:
    fields: list[tuple[str, str]] = []
    if data.get("label"):
        fields.append(("label", _py_string(str(data["label"]))))
    fields.append(("style", f"discord.ButtonStyle.{BUTTON_STYLES.get(data.get('style'), 'primary')}"))
    emoji = _emoji_arg(data.get("emoji"))
    if emoji:
        fields.append(("emoji", emoji))
    if data.get("style") == 5:  # Link
        fields.append(("url", _py_string(str(data.get("url") or ""))))
    elif data.get("custom_id"):
        fields.append(("custom_id", _py_string(str(data["custom_id"]))))
    if data.get("disabled"):
        fields.append(("disabled", "True"))
    return _kv(fields)


def _with_comma(lines: list[str]) -> list[str]:
    lines[-1] += ","
    return lines


def _emit(data: dict, depth: int) -> list[str]:
    """Emit one ui.* expression — port of the TS exporter's emit()."""
    out: list[str] = []

    def open_(expr: str) -> None:
        out.append(f"{_ind(depth)}{expr}")

    def close() -> None:
        out.append(f"{_ind(depth)})")

    def child_lines(child: dict) -> None:
        out.extend(_with_comma(_emit(child, depth + 1)))

    t = data.get("type")

    if t == CONTAINER:
        open_("ui.Container(")
        for child in data.get("components") or []:
            child_lines(child)
        if isinstance(data.get("accent_color"), int):
            out.append(f"{_ind(depth + 1)}accent_colour=0x{data['accent_color']:06x},")
        if data.get("spoiler"):
            out.append(f"{_ind(depth + 1)}spoiler=True,")
        close()
        return out

    if t == SECTION:
        open_("ui.Section(")
        for child in data.get("components") or []:
            if child.get("type") == TEXT_DISPLAY:
                child_lines(child)
        acc = data.get("accessory")
        if acc and acc.get("type") == THUMBNAIL:
            out.append(f"{_ind(depth + 1)}accessory={_emit(acc, 0)[0].strip()},")
        elif acc and acc.get("type") == BUTTON:
            out.append(f"{_ind(depth + 1)}accessory=ui.Button({_py_button_args(acc)}),")
        close()
        return out

    if t == TEXT_DISPLAY:
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        open_(f"ui.TextDisplay({_py_string(content)})")
        return out

    if t == THUMBNAIL:
        fields: list[tuple[str, str]] = []
        if data.get("description"):
            fields.append(("description", _py_string(str(data["description"]))))
        if data.get("spoiler"):
            fields.append(("spoiler", "True"))
        tail = f", {_kv(fields)}" if fields else ""
        url = str((data.get("media") or {}).get("url") or "")
        open_(f"ui.Thumbnail({_py_string(url)}{tail})")
        return out

    if t == MEDIA_GALLERY:
        open_("ui.MediaGallery(")
        for item in data.get("items") or []:
            fields = []
            if item.get("description"):
                fields.append(("description", _py_string(str(item["description"]))))
            if item.get("spoiler"):
                fields.append(("spoiler", "True"))
            tail = f", {_kv(fields)}" if fields else ""
            url = str((item.get("media") or {}).get("url") or "")
            out.append(f"{_ind(depth + 1)}discord.MediaGalleryItem({_py_string(url)}{tail}),")
        close()
        return out

    if t == SEPARATOR:
        fields = []
        if data.get("divider") is False:
            fields.append(("visible", "False"))
        spacing = data.get("spacing")
        if isinstance(spacing, int):
            fields.append(("spacing", f"discord.SeparatorSpacing.{SEPARATOR_SPACING.get(spacing, 'small')}"))
        open_(f"ui.Separator({_kv(fields)})")
        return out

    if t == FILE:
        tail = ", spoiler=True" if data.get("spoiler") else ""
        url = str((data.get("file") or {}).get("url") or "")
        open_(f"ui.File({_py_string(url)}{tail})")
        return out

    if t == ACTION_ROW:
        open_("ui.ActionRow(")
        for child in data.get("components") or []:
            if child.get("type") == BUTTON:
                out.append(f"{_ind(depth + 1)}ui.Button({_py_button_args(child)}),")
            else:
                child_lines(child)
        close()
        return out

    if t == BUTTON:
        open_(f"ui.Button({_py_button_args(data)})")
        return out

    # Select menus: String/User/Role/Mentionable/Channel — all-keyword.
    cls = SELECT_CLASSES.get(t)
    if not cls:
        return out
    open_(f"ui.{cls}(")
    fields = [("custom_id", _py_string(str(data.get("custom_id") or "")))]
    if data.get("placeholder"):
        fields.append(("placeholder", _py_string(str(data["placeholder"]))))
    if isinstance(data.get("min_values"), int):
        fields.append(("min_values", str(data["min_values"])))
    if isinstance(data.get("max_values"), int):
        fields.append(("max_values", str(data["max_values"])))
    if data.get("disabled"):
        fields.append(("disabled", "True"))
    for key, value in fields:
        out.append(f"{_ind(depth + 1)}{key}={value},")
    if data.get("channel_types"):
        names = [f"discord.ChannelType.{CHANNEL_TYPES.get(ct, ct)}" for ct in data["channel_types"]]
        out.append(f"{_ind(depth + 1)}channel_types=[{', '.join(names)}],")
    if data.get("options"):
        out.append(f"{_ind(depth + 1)}options=[")
        for option in data["options"]:
            opt_fields: list[tuple[str, str]] = [
                ("label", _py_string(str(option.get("label") or ""))),
                ("value", _py_string(str(option.get("value") or ""))),
            ]
            if option.get("description"):
                opt_fields.append(("description", _py_string(str(option["description"]))))
            emoji = _emoji_arg(option.get("emoji"))
            if emoji:
                opt_fields.append(("emoji", emoji))
            if option.get("default"):
                opt_fields.append(("default", "True"))
            out.append(f"{_ind(depth + 2)}discord.SelectOption({_kv(opt_fields)}),")
        out.append(f"{_ind(depth + 1)}],")
    close()
    return out


def design_to_code(project: dict) -> str:
    """Validated project state -> a runnable playground script."""
    components = validate_project(project)["tree"]
    lines: list[str] = [
        "# Generated by DiscordEmbeder — Components V2 via ui.LayoutView.",
        "import discord",
        "from discord import ui",
        "",
        "",
        "def build_view() -> ui.LayoutView:",
        "    view = ui.LayoutView(timeout=None)",
    ]
    for component in components:
        lines.append("    view.add_item(")
        lines.extend(_with_comma(_emit(component, 2)))
        lines.append("    )")
    lines.append("    return view")
    lines.extend(["", "", "async def main():", "    await send(view=build_view())", ""])
    return "\n".join(lines)
