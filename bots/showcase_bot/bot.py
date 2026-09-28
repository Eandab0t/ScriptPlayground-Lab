import discord
from discord.ext import commands

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class Panel(discord.ui.View):
    @discord.ui.button(label="Wave", style=discord.ButtonStyle.primary, custom_id="showcase_wave")
    async def wave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"👋 {interaction.user.display_name} waved from the desktop app!", ephemeral=True)


@bot.tree.command(name="panel", description="Show the demo panel")
async def panel(interaction: discord.Interaction):
    await interaction.response.send_message("Control panel ready:", view=Panel())


async def main():
    async with bot:
        await bot.start("offline-simulated-token")
