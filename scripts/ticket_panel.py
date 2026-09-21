"""Ticket panel demo: a support panel that opens private ticket channels.

Shows: channel creation from button handlers, per-ticket state in module
globals, and ephemeral confirmations. Click "🎫 Open ticket" to spawn
#ticket-1, #ticket-2, … each with a welcome embed and a Close button;
closing deletes the ticket channel.
"""

import discord

TICKETS = {}  # ticket number -> channel id
NEXT_TICKET = 1


class TicketControls(discord.ui.View):
    def __init__(self, number):
        super().__init__(timeout=None)
        self.number = number
        self.add_item(
            discord.ui.Button(label="🔒 Close ticket", style=discord.ButtonStyle.danger, custom_id=f"close-{number}")
        )


async def main():
    panel = discord.Embed(
        title="🎫 Support Tickets",
        description="Need help? Open a ticket and a channel will be created for you.",
        color=0xFAA81A,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label="🎫 Open ticket", style=discord.ButtonStyle.success, custom_id="open-ticket")
    )
    await send(embed=panel, view=view)


async def on_click(interaction, custom_id, values):
    global NEXT_TICKET
    if custom_id == "open-ticket":
        number = NEXT_TICKET
        NEXT_TICKET += 1
        channel = await interaction.client.guilds[0].create_text_channel(f"ticket-{number}")
        TICKETS[number] = channel.id
        welcome = discord.Embed(
            title=f"Ticket #{number}",
            description=f"{interaction.user.mention}, describe your issue and the team will reply here.",
            color=0x3BA55D,
        )
        await channel.send(embed=welcome, view=TicketControls(number))
        await interaction.response.send_message(
            f"✅ Opened {channel.mention} for you.", ephemeral=True
        )
    elif custom_id.startswith("close-"):
        number = int(custom_id.split("-")[1])
        channel = interaction.client.get_channel(TICKETS.get(number))
        if channel:
            await channel.delete()
            TICKETS.pop(number, None)
            await interaction.response.send_message(f"🔒 Ticket #{number} closed.", ephemeral=True)
        else:
            await interaction.response.send_message("That ticket is already gone.", ephemeral=True)
