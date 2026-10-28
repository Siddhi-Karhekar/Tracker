"""Discord bot: captures messages live, catches up on missed history, and respects consent.

Run:  python -m teampulse.ingest.discord_bot

Privacy by design:
  * Only one server (DISCORD_GUILD_ID) is read.
  * Messages are stored only for members who typed `!teampulse join`, and only from that point on.
  * `!teampulse leave` opts out and deletes everything stored for that member.
  * Edits and deletes in Discord are mirrored into the database.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from teampulse import config, db
from teampulse.models import Event, issue_refs, to_iso

log = logging.getLogger("teampulse.discord")

PREFIX = "!teampulse"
HELP_TEXT = (
    "**TeamPulse bot** reads this server to track project progress and blockers.\n"
    "`!teampulse join` – allow TeamPulse to store your messages from now on\n"
    "`!teampulse leave` – stop, and delete everything stored from you\n"
    "`!teampulse status` – see how many of your messages are stored\n"
    "Nothing is stored for members who have not joined."
)


# ---- pure mapping (unit-tested without Discord) ------------------------------------------

def channel_label(channel) -> str:
    parent = getattr(channel, "parent", None)
    name = getattr(channel, "name", None) or str(channel.id)
    return f"{parent.name}/{name}" if parent is not None else name


def message_to_event(message) -> Event:
    channel = message.channel
    parent = getattr(channel, "parent", None)
    reference = getattr(message, "reference", None)
    reply_to = f"discord:{reference.message_id}" if reference and reference.message_id else None
    text = message.clean_content
    return Event(
        source="discord",
        kind="message",
        native_id=str(message.id),
        author=str(message.author.id),
        author_name=message.author.display_name,
        body=text,
        url=message.jump_url,
        channel=channel_label(channel),
        parent_id=reply_to,
        created_at=to_iso(message.created_at),
        updated_at=to_iso(message.edited_at or message.created_at),
        extra={
            "channel_id": str(parent.id if parent is not None else channel.id),
            "thread_id": str(channel.id) if parent is not None else None,
            "attachments": len(getattr(message, "attachments", []) or []),
            "issue_refs": issue_refs(text),
        },
    )


def cursor_key(channel_id: int) -> str:
    return f"discord:channel:{channel_id}"


# ---- bot -----------------------------------------------------------------------------------

HEARTBEAT_SECONDS = 300


class TeamPulseBot(discord.Client):
    def __init__(self, guild_id: int, backfill_days: int):
        intents = discord.Intents.default()
        intents.message_content = True      # must also be switched on in the Developer Portal
        super().__init__(intents=intents)
        self.guild_id = guild_id
        self.backfill_days = backfill_days
        self.conn = db.connect()
        self.consented = db.consented_discord_ids(self.conn)

    def _in_scope(self, guild) -> bool:
        return guild is not None and guild.id == self.guild_id

    def _store(self, message) -> bool:
        if message.author.bot or message.content.startswith(PREFIX):
            return False
        if str(message.author.id) not in self.consented:
            return False
        db.upsert_events(self.conn, [message_to_event(message)])
        return True

    # -- startup catch-up --------------------------------------------------------------------
    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        guild = self.get_guild(self.guild_id)
        if guild is None:
            log.error("Bot is not in server %s. Invite it first (see README).", self.guild_id)
            return
        channels = list(guild.text_channels) + list(guild.threads)
        for channel in channels:
            await self.backfill(channel)
        log.info("Backfill done. Listening live. %d member(s) have joined.", len(self.consented))
        if not getattr(self, "_beating", False):
            self._beating = True
            self.loop.create_task(self._heartbeat())

    async def _heartbeat(self):
        while not self.is_closed():          # lets the app show whether the bot is actually running
            db.beat(self.conn, "discord")
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def backfill(self, channel):
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.read_messages and perms.read_message_history):
            return
        key = cursor_key(channel.id)
        last_id = db.get_state(self.conn, key)
        if last_id:
            after = discord.Object(id=int(last_id))
        else:
            after = datetime.now(timezone.utc) - timedelta(days=self.backfill_days)
        stored = seen = 0
        newest = last_id
        try:
            async for message in channel.history(limit=None, after=after, oldest_first=True):
                seen += 1
                stored += self._store(message)
                newest = str(message.id)
        except discord.Forbidden:
            log.warning("No access to #%s, skipping", channel_label(channel))
            return
        if newest:
            db.set_state(self.conn, key, newest)
        if seen:
            log.info("#%s: %d new message(s), %d stored", channel_label(channel), seen, stored)

    # -- live events ---------------------------------------------------------------------------
    async def on_message(self, message):
        if not self._in_scope(message.guild) or message.author.bot:
            return
        if message.content.strip().lower().startswith(PREFIX):
            await self.handle_command(message)
        else:
            self._store(message)
        db.set_state(self.conn, cursor_key(message.channel.id), str(message.id))

    async def on_raw_message_edit(self, payload):
        if payload.guild_id != self.guild_id:
            return
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        self._store(message)

    async def on_raw_message_delete(self, payload):
        if payload.guild_id == self.guild_id:
            db.delete_event(self.conn, f"discord:{payload.message_id}")

    async def on_raw_bulk_message_delete(self, payload):
        if payload.guild_id == self.guild_id:
            for message_id in payload.message_ids:
                db.delete_event(self.conn, f"discord:{message_id}")

    # -- consent commands ----------------------------------------------------------------------
    async def handle_command(self, message):
        parts = message.content.strip().split()
        command = parts[1].lower() if len(parts) > 1 else "help"
        user_id = str(message.author.id)
        name = message.author.display_name

        if command == "join":
            db.set_discord_consent(self.conn, user_id, name, True)
            self.consented.add(user_id)
            await message.reply(
                f"Thanks {name}. TeamPulse will store your messages in this server from now on. "
                "Type `!teampulse leave` any time to stop and delete them."
            )
        elif command == "leave":
            db.set_discord_consent(self.conn, user_id, name, False)
            self.consented.discard(user_id)
            removed = db.delete_discord_messages_of(self.conn, user_id)
            await message.reply(f"Done. You're opted out and {removed} stored message(s) were deleted.")
        elif command == "status":
            stored = db.count_events(self.conn, "discord", user_id)
            state = "joined" if user_id in self.consented else "not joined"
            await message.reply(f"You are **{state}**. Stored messages from you: {stored}.")
        else:
            await message.reply(HELP_TEXT)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = config.require("DISCORD_BOT_TOKEN")
    guild_id = int(config.require("DISCORD_GUILD_ID"))
    bot = TeamPulseBot(guild_id, config.get_int("DISCORD_BACKFILL_DAYS", 30))
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
