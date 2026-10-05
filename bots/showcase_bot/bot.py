import discord
from discord.ext import commands

intents = discord.Intents.default()


class Panel(discord.ui.View):
    @discord.ui.button(label="Wave", style=discord.ButtonStyle.primary, custom_id="showcase_wave")
    async def wave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"👋 {interaction.user.display_name} waved from the desktop app!", ephemeral=True)


class Demo(commands.Cog):
    """Listener-driven reply: proves cog listeners dispatch, not just commands."""

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.content.strip() == "!cog":
            await message.channel.send("cog listener answered")


class ShowcaseBot(commands.Bot):
    async def setup_hook(self):
        """Add the cog here, not at import and not in main().

        ``add_cog`` is a coroutine, so a module-level call is never awaited and
        the cog silently does not exist. setup_hook is the one boot step every
        entry path runs (the runtime calls bot._async_setup_hook() itself when
        it drives login), so the cog is loaded however this bot is started.
        """
        await self.add_cog(Demo())


bot = ShowcaseBot(command_prefix="!", intents=intents)


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Prefix command: proves bot.process_commands over a real MESSAGE_CREATE."""
    await ctx.send("Pong from the worker subprocess.")


@bot.tree.command(name="panel", description="Show the demo panel")
async def panel(interaction: discord.Interaction):
    await interaction.response.send_message("Control panel ready:", view=Panel())


async def main():
    async with bot:
        await bot.start("offline-simulated-token")
