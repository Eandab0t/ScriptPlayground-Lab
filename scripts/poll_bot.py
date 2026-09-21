"""Poll bot demo: /poll creates a live poll, votes update the embed in place.

Shows: slash commands with defaults, select menus, editing your own message
from a component handler, and module-level state (module globals survive
between clicks because handlers share the script's namespace; they reset on
each Run).
"""

import discord


@discord.app_commands.command(name="poll", description="Create a live poll")
async def poll(interaction: discord.Interaction, question: str, options: str = "Yes,No"):
    opts = [o.strip() for o in options.split(",") if o.strip()][:5]
    if len(opts) < 2:
        await interaction.response.send_message("Give me at least two options.", ephemeral=True)
        return
    view = discord.ui.View(timeout=None)
    sel = discord.ui.Select(placeholder="Cast your vote", custom_id="vote", min_values=1, max_values=1)
    for i, opt in enumerate(opts):
        sel.add_option(label=opt, value=str(i), emoji="🗳️")
    view.add_item(sel)
    embed = discord.Embed(
        title=f"📊 {question}",
        description="\n".join(f"**{i}.** {o} — 0 votes" for i, o in enumerate(opts)),
        color=0x5865F2,
    )
    embed.set_author(name="Live poll — vote below")
    await interaction.response.send_message(embed=embed, view=view)


async def on_click(interaction, custom_id, values):
    if custom_id != "vote":
        return
    # Recover the option labels from the message we came from, bump the chosen
    # tally, and edit the embed in place.
    embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
    picked = int(values[0])
    lines = []
    total = 0
    for line in embed.description.split("\n"):
        num, rest = line.split(".", 1)
        idx = int(num.replace("*", ""))
        label = rest.split("—")[0].strip()
        count = int(rest.split("—")[1].strip().split()[0])
        count += 1 if idx == picked else 0
        total += count
        lines.append(f"{num.replace('*', '')}. {label} — {count} vote{'s' if count != 1 else ''}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Total: {total}")
    await interaction.response.edit_message(embed=embed)
