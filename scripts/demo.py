"""Playground demo: embeds, buttons, selects, modals, files, chat, and slash commands.

Press Run, then click the buttons / pick from the menus / open the modal /
type in the composer ("!ping", or "/" for the command palette). Everything
happens locally — no bot token, no Discord connection.
"""

import io
import struct
import zlib

import discord

WELCOME = discord.Embed(
    title="🎮 ScriptPlayground",
    description=(
        "This message was sent by **your code** running locally.\n"
        "Try the buttons, the menu, the form — or type `/` in the composer."
    ),
    color=0x5865F2,
)
WELCOME.add_field(name="Buttons", value="Respond, edit this message, or open a form", inline=True)
WELCOME.add_field(name="Menu", value="Pick a flavor to vote", inline=True)
WELCOME.set_footer(text="Edit this script and press Run to see changes instantly")


async def main():
    print("booting demo…")
    await send(embed=WELCOME, view=DemoView())

    # --- rich embed with real (locally generated) images --------------------
    banner = _png(360, 120, (88, 101, 242))
    icon = _png(64, 64, (59, 165, 93))
    rich = discord.Embed(
        title="🖼️ Rich embed",
        description="Thumbnail and image come from files attached to this message.",
        color=0xFAA81A,
    )
    rich.set_thumbnail(url="attachment://icon.png")
    rich.set_image(url="attachment://banner.png")
    rich.add_field(name="attachment:// refs", value="Resolve against this message's files.", inline=False)
    await send(
        embed=rich,
        files=[
            discord.File(io.BytesIO(banner), "banner.png"),
            discord.File(io.BytesIO(icon), "icon.png"),
        ],
    )

    # --- multi-select + user/role pickers -----------------------------------
    pickers = discord.ui.View(timeout=None)
    pickers.add_item(
        discord.ui.Select(
            placeholder="Pick multiple flavors",
            custom_id="basket",
            min_values=1,
            max_values=3,
            row=0,
            options=[
                discord.SelectOption(label="Vanilla", emoji="🍦", value="vanilla"),
                discord.SelectOption(label="Chocolate", emoji="🍫", value="chocolate"),
                discord.SelectOption(label="Strawberry", emoji="🍓", value="strawberry"),
                discord.SelectOption(label="Mint", emoji="🌿", value="mint"),
            ],
        )
    )
    pickers.add_item(discord.ui.UserSelect(placeholder="Who should get it?", custom_id="user_pick", row=1))
    pickers.add_item(discord.ui.RoleSelect(placeholder="Grant which role?", custom_id="role_pick", row=2))
    await send(content="**Multi-select + user/role pickers** — try them:", view=pickers)

    # --- channels: create them, send between them, switch in the sidebar ----
    announcements = await client.guilds[0].create_text_channel("announcements")
    await announcements.send(
        embed=discord.Embed(
            title="📢 #announcements",
            description="Scripts create channels with `guild.create_text_channel(name)` "
                        "and send with `channel.send(...)` — switch channels in the sidebar.",
            color=0xED4245,
        )
    )
    bot_spam = await client.guilds[0].create_text_channel("bot-spam")
    for i in range(1, 4):
        await bot_spam.send(f"spam message {i} 🎈")


def _png(width: int, height: int, rgb: tuple) -> bytes:
    """A tiny solid-color PNG, built by hand (no dependencies needed)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class DemoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="👋 Say hi", style=discord.ButtonStyle.primary, custom_id="hi"))
        self.add_item(discord.ui.Button(label="🧾 Open form", custom_id="form"))
        self.add_item(
            discord.ui.Select(
                placeholder="Vote for a flavor",
                custom_id="flavor",
                options=[
                    discord.SelectOption(label="Vanilla", emoji="🍦", value="vanilla"),
                    discord.SelectOption(label="Chocolate", emoji="🍫", value="chocolate"),
                    discord.SelectOption(label="Strawberry", emoji="🍓", value="strawberry"),
                ],
            )
        )


async def on_click(interaction, custom_id, values):
    if custom_id == "hi":
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"Hi {interaction.user.mention}! 👋 (ephemeral, just for you)",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    elif custom_id == "form":
        modal = discord.ui.Modal(title="Tell us about you")
        modal.add_item(discord.ui.TextInput(label="Nickname", required=True, max_length=32))
        modal.add_item(discord.ui.TextInput(label="Favorite language", required=False, default="Python"))
        await interaction.response.send_modal(modal)

    elif custom_id == "flavor":
        votes = {"vanilla": 3, "chocolate": 5, "strawberry": 2}
        votes[values[0]] = votes.get(values[0], 0) + 1
        await interaction.response.edit_message(
            content=f"**Flavor votes** — 🍦 {votes['vanilla']} · 🍫 {votes['chocolate']} · 🍓 {votes['strawberry']}"
        )

    elif custom_id == "basket":
        names = {"vanilla": "🍦", "chocolate": "🍫", "strawberry": "🍓", "mint": "🌿"}
        basket = " ".join(names.get(v, v) for v in values)
        await interaction.response.send_message(
            embed=discord.Embed(description=f"Basket ({len(values)}): {basket}", color=0x3BA55D),
            ephemeral=True,
        )

    elif custom_id == "user_pick":
        user_id = int(values[0])
        member = client.get_user(user_id)
        await interaction.response.send_message(
            f"🎁 {interaction.user.mention} picked {member.mention if member else values[0]}!",
            ephemeral=True,
        )

    elif custom_id == "role_pick":
        await interaction.response.send_message(
            f"🏷️ Role <@&{values[0]}> selected (mention resolves against the fixtures).",
            ephemeral=True,
        )


async def on_submit(interaction, values, modal_id):
    fields = list(values.values())
    nickname = fields[0] if fields else "???"
    language = fields[1] if len(fields) > 1 else "unspecified"
    await interaction.response.send_message(
        embed=discord.Embed(
            title="Form received",
            description=f"Nickname: **{nickname}**\nFavorite language: **{language}**",
            color=discord.Color.gold(),
        )
    )


async def on_message(message):
    if message.content.startswith("!ping"):
        await message.channel.send(f"🏓 Pong! (latency {client.latency * 1000:.0f} ms)")
    elif message.content.startswith("!hello"):
        await message.reply(f"Hello, {message.author.mention}!")
    elif message.content.startswith("!here"):
        await message.channel.send(f"You are in **#{message.channel.name}** (id {message.channel.id}).")


# --- slash commands: type "/" in the composer ---------------------------------


@discord.app_commands.command(name="echo", description="Say something back, as the bot")
async def echo(interaction: discord.Interaction, text: str, shout: bool = False):
    out = text.upper() if shout else text
    await interaction.response.send_message(f"🗣️ {out}")


@discord.app_commands.command(name="roll", description="Roll an N-sided die")
async def roll(interaction: discord.Interaction, sides: int = 6):
    import random

    await interaction.response.send_message(f"🎲 Rolled **{random.randint(1, sides)}** on a d{sides}.")


@discord.app_commands.command(name="greet", description="Greet a member (or the whole server)")
async def greet(interaction: discord.Interaction, who: discord.Member = None, tier: str = "wave"):
    # `tier` is autocomplete-driven: plain annotation + @greet.autocomplete below
    # (a Choice[str] annotation + autocomplete together is a discord.py error).
    target = who.mention if who else "everyone"
    if tier == "salute":
        await interaction.response.send_message(f"🫡 Saluting {target}!")
    elif tier == "hug":
        await interaction.response.send_message(f"🤗 Hugging {target}!")
    else:
        await interaction.response.send_message(f"👋 Waving at {target}!")


@greet.autocomplete("tier")
async def greet_tier(interaction: discord.Interaction, current: str):
    names = ["wave", "salute", "hug"]
    return [
        discord.app_commands.Choice(name=n, value=n)
        for n in names
        if n.startswith((current or "").lower())
    ][:25]
