import discord


async def main():
    embed = discord.Embed(
        title="Folder bot",
        description="Edit bots/demo_bot/bot.py, then reconnect or run it.",
        color=0x5865F2,
    )
    await send(content="Connected from a bot folder.", embed=embed)
