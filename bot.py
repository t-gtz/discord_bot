import asyncio
import os
import itertools

import discord
import spotipy
import yt_dlp

from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyClientCredentials

load_dotenv()

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YTDL_OPTS = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}
 
SPECIAL_USER_ID = 936929561302675456
# [] convert to variable and add an admin command to enter/edit the id/user

#
# Music Player
#

class MusicPlayer:
    """Manages the queue and playback for a single guild."""
 
    def __init__(self, bot: commands.Bot, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.queue: list[dict] = []
        self.current_song: str | None = None
        
    def enqueue(self, item: dict) -> None:
        self.queue.append(item)

    async def play_next(self) -> None:
        if not self.queue:
            self.current_song = None
            return
 
        item = self.queue.pop(0)
        self.current_song = item["title"]
 
        url = await self._resolve_url(item)
        if url is None:
            self._schedule_next()
            return
 
        voice_client = self.bot.get_guild(self.guild_id).voice_client
        if voice_client and voice_client.is_connected():
            try:
                source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTS)
                voice_client.play(source, after=lambda _: self._schedule_next())
            except Exception as exc:
                print(f"[MusicPlayer] Playback error: {exc}")
                self._schedule_next()

    def _schedule_next(self) -> None:
        asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

    async def _resolve_url(self, item: dict) -> str | None:
        if item["type"] == "url":
            return item["data"]
    
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._search, item["data"])
            return result
        except Exception as exc:
            print(f"[MusicPlayer] Search resolve error: {exc}")
            return None

    @staticmethod
    def _search(query: str) -> str | None:
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch:{query}", download=False)
                entry = (info.get("entries") or [None])[0]
                if entry:
                    return entry.get("url") or entry.get("webpage_url")
            except Exception as exc:
                print(f"[MusicPlayer] yt-dlp search error: {exc}")
        return None
    
    async def stop(self) -> None:
        self.queue.clear()
        self.current_song = None
        guild = self.bot.get_guild(self.guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect()

#
# Music Cog
#

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._players: dict[int, MusicPlayer] = {}
        self._connecting: set[int] = set()
 
        self._play_messages = itertools.cycle([
            "Moment… ich leg Ihnen mal schnell die _*{title}*_ Kassette ein",
            "Aha, Sie wollen _*{title}*_ hören! Na dann, ich drück mal auf \"Plee\"",
            "*ping* *pong* Ihr Lied _*{title}*_ wurde schneller gespielt als der Aufschlag zurückgekommen ist!",
        ])
        self._stop_messages = itertools.cycle([
            "Das Turnier ist zuende… Mit dem Schläger hab ich seit 1987 nicht mehr verloren",
            "Musik aus! Und Sie gehen jetzt nach vorne und rechnen das nochmal vor",
            "So, Feierabend!",
        ])
 
        # Spotify
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.sp = (
            spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=client_id, client_secret=client_secret
                )
            )
            if client_id and client_secret
            else None
        )
        if not self.sp:
            print("[MusicCog] Spotify credentials missing — Spotify support disabled.")


    def _get_player(self, guild_id: int) -> MusicPlayer:
        if guild_id not in self._players:
            self._players[guild_id] = MusicPlayer(self.bot, guild_id)
        return self._players[guild_id]
 
    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        """Connect to the user's voice channel; guard against concurrent connects."""
        existing = interaction.guild.voice_client
        if existing and existing.is_connected():
            return existing
 
        guild_id = interaction.guild.id
        if guild_id in self._connecting:
            await interaction.followup.send("🚧 Already connecting — try again in a second.")
            return None
 
        self._connecting.add(guild_id)
        try:
            return await interaction.user.voice.channel.connect()
        except Exception as exc:
            await interaction.followup.send(f"Could not connect to voice channel: {exc}")
            return None
        finally:
            self._connecting.discard(guild_id)
 
    @staticmethod
    def _detect_spotify_type(url: str) -> str | None:
        for kind in ("track", "playlist", "album"):
            if f"spotify.com/{kind}/" in url or f"spotify:{kind}:" in url:
                return kind
        return None
 
    def _enqueue_spotify(self, player: MusicPlayer, url: str, resource_type: str) -> int:
        if resource_type == "track":
            track = self.sp.track(url)
            name = f"{track['artists'][0]['name']} - {track['name']}"
            player.enqueue({"type": "search", "data": name, "title": name})
            return 1
 
        fetcher = {"playlist": self.sp.playlist_items, "album": self.sp.album_tracks}[resource_type]
        results = fetcher(url)
        items = list(results.get("items", []))
        while results.get("next"):
            results = self.sp.next(results)
            items.extend(results.get("items", []))
 
        count = 0
        for item in items:
            track = item.get("track") if resource_type == "playlist" else item
            if not track:
                continue
            name = f"{track['artists'][0]['name']} - {track['name']}"
            player.enqueue({"type": "search", "data": name, "title": name})
            count += 1
        return count

#
# Commands
#

    @app_commands.command(name="play", description="Play a song or add to queue")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()

        if not interaction.user.voice:
            await interaction.followup.send("You're not in a voice channel.")
            return

        voice_client = await self._connect_voice(interaction)
        if voice_client is None:
            return

        player = self.get_player(interaction.guild.id)
        spotify_type, _ = self._extract_spotify_type(query)

        if spotify_type:
            if not self.sp:
                await interaction.followup.send("Spotify is not configured.")
                return
            try:
                added = self._enqueue_spotify(player, query, spotify_type)
            except Exception as error:
                await interaction.followup.send(f"Spotify error: {error}")
                return 
            if added == 0:
                    await interaction.followup.send(f"No playable tracks found in Spotify {spotify_type}.")
                    return
            await interaction.followup.send(f"Added **{added}** Spotify {spotify_type} track(s) to queue.")

        else:
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(
                    None,
                    lambda: yt_dlp.YoutubeDL(YTDL_OPTS).extract_info(query, download=False),
                )
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                url = info.get("url") or info.get("webpage_url")
                title = info.get("title", "Unknown")
                if not url:
                    raise ValueError("No playable URL found.")
                player.enqueue({"type": "url", "data": url, "title": title})
                await interaction.followup.send(f"Added to queue: **{title}**")
            except Exception as exc:
                await interaction.followup.send("Failed to find or play the track.")
                print(f"[MusicCog] yt-dlp error: {exc}")
                return
 
        if not voice_client.is_playing():
            await player.play_next()
            if player.current_song:
                msg = next(self._play_messages).format(title=player.current_song)
                await interaction.followup.send(msg)


    @app_commands.command(name="stop", description="Stop the playing song and clear the queue")
    async def stop(self, interaction: discord.Interaction) -> None:
        if interaction.user.id == SPECIAL_USER_ID:
            await interaction.response.send_message("LECK EI!")
            return
        
        if not interaction.guild.voice_client:
            await interaction.followup.send("I'm not connected to any voice channel.")
            return

        player = self.get_player(interaction.guild.id)
        await player.stop()
        await interaction.response.send_message(next(self._stop_messages))


    @app_commands.command(name="hello", description="Say hello... but not to the bot")
    async def hello(self, interaction: discord.Interaction, message: str, user: discord.User = None, user_id: str = None) -> None:
        await interaction.response.defer(ephemeral=True)
        
        target = user
        if target is None:
            if user_id is None:
                await interaction.followup.send("Provide a user or a user ID.")
                return
            try:
                target = await self.bot.fetch_user(int(user_id))
            except ValueError:
                await interaction.followup.send("Invalid user ID format.")
                return
            except discord.NotFound:
                await interaction.followup.send("No user found with that ID.")
                return
            except discord.HTTPException as error:
                await interaction.followup.send(f"Failed to fetch user: {error}")
                return
            
        try:
            await target.send(message)
            await interaction.followup.send(f"DM sent to **{target.name}**: \"{message}\"")
        except discord.Forbidden:
            await interaction.followup.send("Can't DM that user (they may have DMs diabled).")
        except discord.HTTPException as error:
            await interaction.followup.send(f"Failed to send DM: {error}")

#
# Bot
#

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(MusicCog(self))
        await self.tree.sync()

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        print(f'Opus loaded: {discord.opus.is_loaded()}')

if __name__ == "__main__":
    MusicBot().run(os.getenv('DISCORD_TOKEN'))
