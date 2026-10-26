"""TeamPulse Discord bot: live listening + history backfill → SQLite.

    python -m teampulse.ingest.discord_bot

Setup (Developer Portal → your app → Bot):
  * turn on MESSAGE CONTENT INTENT (privileged, but no verification needed under 100 servers)
  * invite with scopes `bot` and permissions: View Channels + Read Message History (+ Send Messages for replies)

Behaviour
  * on start: for each allowed channel (and its active threads), fetch everything after the
    last stored message id, so nothing is missed while the bot was offline
  * on message / edit / delete: store, update, or remove
  * commands (any channel it reads):
        !teampulse status   what the bot stores and for whom
        !teampulse optout   stop storing my messages and delete the ones already stored
        !teampulse optin    start storing my messages again
"""
from __future__ import annotations

import asyncio
import logging
import sys

import discord

from teampulse import db
from teampulse.config import env, load_team
from teampulse.ingest import discord_ingest

log = logging.getLogger("teampulse.discord")


ACCEPT_WEBHOOKS = (env("DISCORD_ACCEPT_WEBHOOKS", "0") or "0") == "1"  # for the seeded demo server


def to_export_dict(m: discord.Message) -> dict:
    """discord.py Message → DiscordChatExporter-shaped dict (what discord_ingest expects)."""
    is_bot = m.author.bot and not (ACCEPT_WEBHOOKS and m.webhook_id)
    kind = {discord.MessageType.default: "Default", discord.MessageType.reply: "Reply",
            discord.MessageType.thread_starter_message: "ThreadStarterMessage"}.get(m.type, str(m.type))
    out = {
        "id": str(m.id),
        "type": kind,
        "timestamp": m.created_at.isoformat(),
        "timestampEdited": m.edited_at.isoformat() if m.edited_at else None,
        "content": m.content,
        "author": {"id": None if m.webhook_id else str(m.author.id), "name": m.author.name, "isBot": is_bot,
                   "nickname": getattr(m.author, "display_name", m.author.name)},
        "mentions": [{"id": str(u.id), "name": u.name} for u in m.mentions],
        "attachments": [{"fileName": a.filename, "url": a.url} for a in m.attachments],
    }
    if m.reference and m.reference.message_id:
        out["reference"] = {"messageId": str(m.reference.message_id)}
    return out


class TeamPulseBot(discord.Client):
    def __init__(self, guild_id: int, channels: set[str]):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.guild_id = guild_id
        self.channel_names = channels
        self.team = load_team()
        self.conn = db.connect()
        db.sync_members(self.conn, self.team)
        self.lock = asyncio.Lock()

    # ---------------------------------------------------------------- helpers
    def allowed(self, channel) -> bool:
        if getattr(channel, "guild", None) is None or channel.guild.id != self.guild_id:
            return False
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        return not self.channel_names or base.name in self.channel_names

    def channel_label(self, channel) -> tuple[str, str | None]:
        if isinstance(channel, discord.Thread):
            return channel.parent.name, channel.name
        return channel.name, None

    def store(self, m: discord.Message) -> bool:
        name, thread = self.channel_label(m.channel)
        return discord_ingest.ingest_message(
            self.conn, self.team, to_export_dict(m), name, str(m.guild.id), str(m.channel.id),
            skip=db.opted_out(self.conn), thread=thread)

    # ---------------------------------------------------------------- backfill
    async def backfill_channel(self, ch) -> int:
        scope = str(ch.id)
        cursor = db.get_cursor(self.conn, "discord", scope)
        after = discord.Object(id=int(cursor)) if cursor else None
        n, last = 0, cursor
        try:
            async for m in ch.history(limit=None, after=after, oldest_first=True):
                async with self.lock:
                    if self.store(m):
                        n += 1
                last = str(m.id)
                if n and n % 200 == 0:
                    self.conn.commit()
        except discord.Forbidden:
            log.warning("No permission to read history in #%s", ch.name)
            return 0
        if last:
            db.set_cursor(self.conn, "discord", scope, last)
        self.conn.commit()
        return n

    async def on_ready(self):
        guild = self.get_guild(self.guild_id)
        if guild is None:
            log.error("Bot is not in guild %s. Invite it first.", self.guild_id)
            await self.close()
            return
        log.info("Connected as %s to %s; backfilling…", self.user, guild.name)
        total = 0
        for ch in guild.text_channels:
            if self.allowed(ch):
                total += await self.backfill_channel(ch)
        for th in await guild.active_threads():
            if self.allowed(th):
                total += await self.backfill_channel(th)
        linked = discord_ingest.link_hash_refs(self.conn)
        log.info("Backfill done: %d new messages, %d linked by #refs. Listening live.", total, linked)

    # ---------------------------------------------------------------- live events
    async def on_message(self, m: discord.Message):
        if (m.author.bot and not (ACCEPT_WEBHOOKS and m.webhook_id)) or not self.allowed(m.channel):
            return
        if m.content.strip().lower().startswith("!teampulse"):
            await self.command(m)
            return
        async with self.lock:
            if self.store(m):
                db.set_cursor(self.conn, "discord", str(m.channel.id), str(m.id))
                self.conn.commit()

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        ch = self.get_channel(payload.channel_id)
        if ch is None or not self.allowed(ch):
            return
        try:
            m = await ch.fetch_message(payload.message_id)  # always fetch: the cached copy is pre-edit
        except (discord.NotFound, discord.Forbidden):
            return
        async with self.lock:
            self.store(m)
            self.conn.commit()

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        async with self.lock:
            eid = f"discord:message:{payload.message_id}"
            self.conn.execute("DELETE FROM claims WHERE source_ref=?", (eid,))
            self.conn.execute("DELETE FROM events WHERE event_id=?", (eid,))
            self.conn.commit()

    # ---------------------------------------------------------------- commands
    async def command(self, m: discord.Message):
        parts = m.content.split()
        cmd = parts[1].lower() if len(parts) > 1 else "status"
        handle = self.team.from_discord(str(m.author.id), m.author.name)
        known = self.team.get(handle) is not None
        if cmd == "optout":
            async with self.lock:
                self.conn.execute("INSERT OR REPLACE INTO optouts VALUES (?, ?)", (handle, db.now_iso()))
                removed = discord_ingest.purge_member(self.conn, handle)
            await m.reply(f"Done. I won't store your messages, and I deleted {removed} I had stored.")
        elif cmd == "optin":
            async with self.lock:
                self.conn.execute("DELETE FROM optouts WHERE handle=?", (handle,))
                self.conn.commit()
            await m.reply("You're opted in again. Messages from now on will be stored "
                          "(earlier ones are not re-fetched).")
        else:
            n = self.conn.execute("SELECT COUNT(*) FROM events WHERE source='discord'").fetchone()[0]
            mine = self.conn.execute("SELECT COUNT(*) FROM events WHERE source='discord' AND author=?",
                                     (handle,)).fetchone()[0]
            status = "opted out" if handle in db.opted_out(self.conn) else "opted in"
            note = "" if known else " (you're not in team.toml yet)"
            await m.reply(f"TeamPulse reads only this project server and stores messages locally for the team "
                          f"tracker. Stored: {n} messages, {mine} of yours. You are {status}{note}. "
                          f"Commands: `!teampulse optout`, `!teampulse optin`.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token, guild = env("DISCORD_BOT_TOKEN"), env("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID in .env")
    channels = {c.strip() for c in (env("DISCORD_CHANNELS", "") or "").split(",") if c.strip()}
    TeamPulseBot(int(guild), channels).run(token, log_handler=None)


if __name__ == "__main__":
    main()
