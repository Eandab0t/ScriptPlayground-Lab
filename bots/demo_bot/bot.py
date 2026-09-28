import discord
from discord.ext import commands

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.tree.command(name="hello", description="Say hello from the folder bot")
async def hello(interaction: discord.Interaction):
    embed = discord.Embed(title="Folder bot", description="Running from bots/demo_bot — offline, no network.",
                          color=0x5865F2)
    await interaction.response.send_message("Hello from a bot folder! 👋", embed=embed)


async def main():
    async with bot:
        await bot.start("offline-simulated-token")
