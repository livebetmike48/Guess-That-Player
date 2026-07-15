import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Literal

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import mlb_api
import video
import storage
import matching

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guess_game")

intents = discord.Intents.default()

# Post times (UTC): pitcher 17:00 = 1 PM ET, batter 20:00 = 4 PM ET
MODES = {
    "pitcher": {"channel_key": "pitcher_channel_id", "emoji": "🔴", "post_hour_utc": 17,
                 "game_name": "Guess That Pitcher", "prompt": "who's this mystery pitcher?"},
    "batter": {"channel_key": "batter_channel_id", "emoji": "🔵", "post_hour_utc": 20,
                "game_name": "Guess That Batter", "prompt": "who's this mystery hitter?"},
}


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


def build_daily_message(mode: str, yesterday_game: dict | None) -> str:
    cfg = MODES[mode]
    lines = []
    if yesterday_game:
        lines.append(f"Yesterday's {cfg['game_name']} was **{yesterday_game['player_name']}**.")
        lines.append("")
    lines.append(f"**{cfg['game_name']}!** — {cfg['prompt']}")
    lines.append("Guess with `/guess <name>` — one guess each.")
    return "\n".join(lines)


def build_tracker_text(mode: str, game_date: str) -> str:
    cfg = MODES[mode]
    guesses = storage.get_guesses(game_date, mode)
    lines = [f"{cfg['emoji']} **{cfg['game_name']} — today's guesses**"]
    if not guesses:
        lines.append("*No guesses yet.*")
    for g in guesses:
        mark = "✅" if g["correct"] else "❌"
        lines.append(f"{mark} {g['user_name']}")

    leaderboard = storage.get_leaderboard()
    if leaderboard:
        lines.append("")
        lines.append("**Leaderboard**")
        medals = ["🥇", "🥈", "🥉"]
        for i, entry in enumerate(leaderboard[:5]):
            medal = medals[i] if i < len(medals) else "▫️"
            total = entry["total"]
            pct = round(entry["wins"] / total * 100) if total else 0
            lines.append(f"{medal} **{entry['user_name']}** — {entry['wins']}-{entry['losses']} ({pct}%)")
    return "\n".join(lines)


class GuessGameBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        storage.init_db()

        guess_cmd = app_commands.Command(
            name="guess",
            description="Guess today's mystery player (run in the game's channel)",
            callback=self._guess_callback,
        )
        self.tree.add_command(guess_cmd)

        setpitcher_cmd = app_commands.Command(
            name="setpitcherchannel",
            description="Set this channel for the daily mystery PITCHER game",
            callback=self._setpitcher_callback,
        )
        self.tree.add_command(setpitcher_cmd)

        setbatter_cmd = app_commands.Command(
            name="setbatterchannel",
            description="Set this channel for the daily mystery BATTER game",
            callback=self._setbatter_callback,
        )
        self.tree.add_command(setbatter_cmd)

        reset_cmd = app_commands.Command(
            name="resetleaderboard",
            description="ADMIN: wipe the leaderboard and all guess history",
            callback=self._resetleaderboard_callback,
        )
        self.tree.add_command(reset_cmd)

        cleartoday_cmd = app_commands.Command(
            name="cleartoday",
            description="ADMIN: discard today's posted games (and their guesses) so posts can run fresh",
            callback=self._cleartoday_callback,
        )
        self.tree.add_command(cleartoday_cmd)

        postnow_cmd = app_commands.Command(
            name="postnow",
            description="Manually post today's games right now (for testing/late setup)",
            callback=self._postnow_callback,
        )
        self.tree.add_command(postnow_cmd)

        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    def _mode_for_channel(self, channel_id: int) -> str | None:
        for mode, cfg in MODES.items():
            saved = storage.get_config(cfg["channel_key"])
            if saved and int(saved) == channel_id:
                return mode
        return None

    async def _guess_callback(self, interaction: discord.Interaction, name: str):
        mode = self._mode_for_channel(interaction.channel_id)
        if mode is None:
            await interaction.response.send_message(
                "This channel isn't set up for a guessing game -- use /setpitcherchannel or /setbatterchannel first.",
                ephemeral=True,
            )
            return

        today = et_date_str(0)
        game = storage.get_game(today, mode)
        if game is None:
            await interaction.response.send_message("No mystery player posted today yet -- check back later!", ephemeral=True)
            return

        correct = matching.guess_matches(name, game["player_name"])
        recorded = storage.add_guess(
            today, mode, str(interaction.user.id), interaction.user.display_name, name, correct
        )
        if not recorded:
            await interaction.response.send_message("You already used your one guess today!", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Your guess is locked in{' ✅ Correct!' if correct else ' ❌ Not it!'}",
            ephemeral=True,
        )
        await self._update_tracker(mode, today, interaction.channel)

    async def _update_tracker(self, mode: str, game_date: str, channel):
        game = storage.get_game(game_date, mode)
        text = build_tracker_text(mode, game_date)
        tracker_id = game.get("tracker_message_id") if game else None
        try:
            if tracker_id:
                msg = await channel.fetch_message(int(tracker_id))
                await msg.edit(content=text)
            else:
                msg = await channel.send(text)
                storage.set_tracker_message(game_date, mode, str(msg.id))
        except Exception as e:
            log.error("Tracker update failed (%s %s): %s", mode, game_date, e)

    async def _setpitcher_callback(self, interaction: discord.Interaction):
        storage.set_config("pitcher_channel_id", str(interaction.channel_id))
        await interaction.response.send_message("✅ Daily mystery PITCHER game will post here.")

    async def _setbatter_callback(self, interaction: discord.Interaction):
        storage.set_config("batter_channel_id", str(interaction.channel_id))
        await interaction.response.send_message("✅ Daily mystery BATTER game will post here.")

    async def _resetleaderboard_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        storage.clear_all_guesses()
        await interaction.response.send_message("🧹 Leaderboard wiped -- fresh start for everyone.")

    async def _cleartoday_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        today = et_date_str(0)
        storage.clear_games_for_date(today)
        await interaction.response.send_message(
            f"🧹 Today's games ({today}) discarded -- the scheduled posts (or /postnow) will post fresh ones."
        )

    async def _postnow_callback(self, interaction: discord.Interaction,
                                 mode: Literal["pitcher", "batter", "both"] = "both"):
        await interaction.response.defer()
        only = None if mode == "both" else mode
        results = await post_daily_games(self, only_mode=only)
        friendly = {
            "posted": "✅ posted",
            "already_posted": "ℹ️ already posted today (won't double-post)",
            "not_configured": "⚠️ channel not set",
            "channel_missing": "⚠️ saved channel not found",
            "no_highlight": "❌ no suitable highlight found",
            "video_failed": "❌ video processing failed (check logs)",
            "send_failed": "❌ posting failed (check logs)",
        }
        lines = [f"**{MODES[m]['game_name']}**: {friendly.get(status, status)}" for m, status in results.items()]
        await interaction.followup.send("\n".join(lines))

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not daily_post.is_running():
            daily_post.start(self)


client = GuessGameBot()


async def post_daily_games(bot: GuessGameBot, only_mode: str | None = None) -> dict:
    """Posts games. Returns {mode: status} for honest reporting."""
    today = et_date_str(0)
    yesterday = et_date_str(-1)
    results = {}

    for mode, cfg in MODES.items():
        if only_mode and mode != only_mode:
            continue
        channel_id = storage.get_config(cfg["channel_key"])
        if not channel_id:
            results[mode] = "not_configured"
            continue
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            results[mode] = "channel_missing"
            continue
        if storage.get_game(today, mode):
            results[mode] = "already_posted"
            continue

        try:
            highlight = await asyncio.to_thread(mlb_api.pick_daily_highlight, yesterday, mode == "pitcher")
        except Exception as e:
            log.error("Highlight pick failed for %s: %s", mode, e)
            results[mode] = "no_highlight"
            continue
        if highlight is None:
            log.warning("No suitable %s highlight found (searched season)", mode)
            results[mode] = "no_highlight"
            continue

        clip_path = f"/tmp/guess_{mode}_{today}.mp4"
        ok = await asyncio.to_thread(video.make_blurred_clip, highlight["mp4_url"], clip_path)
        if not ok:
            log.error("Video processing failed for %s (%s)", mode, highlight["title"])
            results[mode] = "video_failed"
            continue

        yesterday_game = storage.get_game(yesterday, mode)
        storage.save_game(today, mode, highlight["player_id"], highlight["player_name"], highlight["title"])

        try:
            message_text = build_daily_message(mode, yesterday_game)
            await channel.send(message_text, file=discord.File(clip_path))
            await bot._update_tracker(mode, today, channel)
            results[mode] = "posted"
            log.info("Posted %s game: answer is %s", mode, highlight["player_name"])
        except Exception as e:
            log.error("Posting %s game failed: %s", mode, e)
            results[mode] = "send_failed"
        finally:
            try:
                os.unlink(clip_path)
            except OSError:
                pass
    return results


# Fires at both post times; each run posts whichever game matches the hour
# (pitcher at 17:00 UTC = 1 PM ET, batter at 20:00 UTC = 4 PM ET).
@tasks.loop(time=[dtime(hour=17, minute=0), dtime(hour=20, minute=0)])
async def daily_post(bot: GuessGameBot):
    try:
        hour = datetime.now(timezone.utc).hour
        mode = next((m for m, cfg in MODES.items() if cfg["post_hour_utc"] == hour), None)
        if mode:
            await post_daily_games(bot, only_mode=mode)
    except Exception as e:
        log.error("daily_post cycle failed, will retry next scheduled run: %s", e)


@daily_post.before_loop
async def before_daily_post():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
