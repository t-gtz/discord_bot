import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import os
import itertools
from dotenv import load_dotenv

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# Load environment variables
load_dotenv()

class MusicPlayer:
    """Manages music playback and queue for a specific guild."""
    def __init__(self, bot, guild_id):
        self.bot = bot
        self.guild_id = guild_id
        self.guild_id = guild_id
        self.queue = [] # List of dictionaries: {'type': 'url'|'search', 'data': url|query, 'title': title}
        self.voice_client = None
        self.current_song = None
        
        # FFmpeg options for streaming
        self.ffmpeg_opts = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }
        
        # yt-dlp options
        self.ytdl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
            'restrictfilenames': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'auto',
            'source_address': '0.0.0.0'
        }

    async def add_to_queue(self, item):
        """Adds a song/search query to the queue. item is a dict."""
        self.queue.append(item)

    async def play_next(self):
        """Plays the next song in the queue."""
        if not self.queue:
            self.current_song = None
            return

        item = self.queue.pop(0)
        self.current_song = item['title']

        url = None
        if item['type'] == 'url':
             url = item['data']
        elif item['type'] == 'search':
             # Resolve search query to URL immediately before playing
             # using a fresh yt-dlp instance or the loop. 
             # Since we need it async, we can offload to loop or run blocking if quick.
             # Ideally run in executor.
             try:
                loop = asyncio.get_event_loop()
                # Run blocking yt-dlp search in executor
                info = await loop.run_in_executor(None, lambda: self._resolve_search(item['data']))
                if info:
                    url = info['url']
                else:
                    print(f"Could not resolve search: {item['data']}")
                    self.schedule_next()
                    return
             except Exception as e:
                 print(f"Error resolving search {item['data']}: {e}")
                 self.schedule_next()
                 return

        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return
            
        self.voice_client = guild.voice_client
        
        if self.voice_client and self.voice_client.is_connected():
            try:
                source = discord.FFmpegPCMAudio(url, **self.ffmpeg_opts)
                self.voice_client.play(source, after=lambda e: self.schedule_next())
            except Exception as e:
                print(f"Error playing audio: {e}")
                self.schedule_next()

    def schedule_next(self):
        """Schedules the next song to be played safely on the main loop."""
        asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

    async def stop(self):
        """Stops playback and clears the queue."""
        self.queue = []
        self.current_song = None
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None

    def _resolve_search(self, query):
        """Helper to resolve a search query to a playable entry using yt-dlp."""
        with yt_dlp.YoutubeDL(self.ytdl_opts) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch:{query}", download=False)
                if 'entries' in info and len(info['entries']) > 0:
                    entry = info['entries'][0]
                    # Ensure entry has usable stream URL
                    if 'url' in entry and entry['url']:
                        return entry
                    # fallback to webpage_url if needed (yt-dlp can handle it in FFmpegPCMAudio too)
                    if 'webpage_url' in entry:
                        return {'url': entry['webpage_url'], 'title': entry.get('title', query)}
            except Exception as e:
                print(f"Error in _resolve_search: {e}")
        return None

    async def get_queue_list(self):
        """Returns a formatted list of songs in the queue."""
        return [f"{idx + 1}. {item['title']}" for idx, item in enumerate(self.queue)]

class MusicCog(commands.Cog):
    """Cog for music commands."""
    def __init__(self, bot):
        self.bot = bot
        self.players = {} # Map guild_id to MusicPlayer instance
        self.special_user_id = 936929561302675456
        self.play_cycle = itertools.cycle(range(3))
        self.queue_cycle = itertools.cycle(range(3))
        self.stop_cycle = itertools.cycle(range(3))
        self._voice_connecting = set()  # guild IDs currently connecting to voice

        # Initialize Spotipy
        client_id = os.getenv('SPOTIFY_CLIENT_ID')
        client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
        if client_id and client_secret:
            self.sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=client_id, client_secret=client_secret))
        else:
            self.sp = None
            print("Spotify credentials not found in env. Spotify support disabled.")

    def get_player(self, guild_id):
        """Retrieves or creates a MusicPlayer for the guild."""
        if guild_id not in self.players:
            self.players[guild_id] = MusicPlayer(self.bot, guild_id)
        return self.players[guild_id]

    async def _connect_voice(self, interaction: discord.Interaction):
        """Connect to the user's voice channel once per guild to prevent race conditions."""
        guild_id = interaction.guild.id
        current_vc = interaction.guild.voice_client
        if current_vc and current_vc.is_connected():
            return current_vc

        if guild_id in self._voice_connecting:
            await interaction.followup.send("🚧 Already connecting to voice. Please try again in a second.")
            return None

        self._voice_connecting.add(guild_id)
        try:
            return await interaction.user.voice.channel.connect()
        except Exception as e:
            await interaction.followup.send(f"Failed to connect to voice channel: {type(e).__name__}: {e}")
            print(f"Voice connect error for guild {guild_id}:", e)
            return None
        finally:
            self._voice_connecting.discard(guild_id)

    def _add_spotify_tracks(self, player, query, resource_type):
        """Populate queue from Spotify resource (track, playlist, album)."""
        count = 0

        if resource_type == "track":
            track = self.sp.track(query)
            artist = track['artists'][0]['name']
            title = track['name']
            player.queue.append({'type': 'search', 'data': f"{artist} - {title}", 'title': f"{artist} - {title}"})
            return 1

        fetcher = {
            'playlist': self.sp.playlist_items,
            'album': self.sp.album_tracks,
        }[resource_type]

        results = fetcher(query)
        items = results.get('items', [])

        while results.get('next'):
            results = self.sp.next(results)
            items.extend(results.get('items', []))

        for item in items:
            track = item.get('track') if resource_type == 'playlist' else item
            if not track:
                continue
            artist = track['artists'][0]['name']
            title = track['name']
            player.queue.append({'type': 'search', 'data': f"{artist} - {title}", 'title': f"{artist} - {title}"})
            count += 1

        return count

    @app_commands.command(name="play", description="Play a song or add to queue")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        if not interaction.user.voice:
            await interaction.followup.send("You're not in a voice channel.")
            return

        voice_client = await self._connect_voice(interaction)
        if voice_client is None:
            return

        player = self.get_player(interaction.guild.id)

        spotify_type, _ = self._extract_spotify_resource(query)

        if spotify_type:
            if not self.sp:
                await interaction.followup.send("Spotify credentials are not configured. Cannot resolve Spotify links.")
                return

            try:
                added = self._add_spotify_tracks(player, query, spotify_type)
                if added == 0:
                    await interaction.followup.send(f"No playable tracks found in Spotify {spotify_type}.")
                    return
                await interaction.followup.send(f"Added **{added}** Spotify {spotify_type} tracks to queue.")
            except Exception as e:
                await interaction.followup.send(f"Error processing Spotify link: {e}")
                print(f"Spotify Error: {e}")
                return

        else:
            try:
                with yt_dlp.YoutubeDL(player.ytdl_opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                    if 'entries' in info and info['entries']:
                        info = info['entries'][0]

                url = info.get('url') or info.get('webpage_url')
                title = info.get('title', 'Unknown')

                if not url:
                    raise ValueError('No playable URL extracted')

                await player.add_to_queue({'type': 'url', 'data': url, 'title': title})
                await interaction.followup.send(f"Added to queue: **{title}**")
            except Exception as e:
                await interaction.followup.send("Failed to find or play the track.")
                print(f"YT-DLP Error: {e}")
                return

        if not voice_client.is_playing():
            await player.play_next()

            if player.current_song:
                msg_options = [
                    f"Moment… ich leg Ihnen mal schnell die _*{player.current_song}*_ Kassette ein",
                    f"Aha, Sie wollen _*{player.current_song}*_ hören! Na dann, ich drück mal auf \"Plee\"",
                    f"*ping* *pong* Ihr Lied _*{player.current_song}*_ wurde schneller gespielt als der Aufschlag zurückgekommen ist!"
                ]
                await interaction.followup.send(msg_options[next(self.play_cycle)])
        else:
            # queue updated while already playing
            return

    @app_commands.command(name="queue", description="Show the current song queue")
    async def show_queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        queue_list = await player.get_queue_list()
        
        if not queue_list:
            await interaction.response.send_message("The queue is empty.")
        else:
            queue_text = "\n".join(queue_list)
            
            responses = [
                "Die Warteschlange ist ja spannender als meine Sains-Fiktschen-Serie",
                "Gucken sie sich die Liste an… hübsch sortiert, fast wie meine Fahrrad Equipment",
                "Da brauchen sie gar nicht Tschat-G-P-T zu fragen was als nächstes läuft"
            ]
            
            await interaction.response.send_message(f"{responses[next(self.queue_cycle)]}:\n{queue_text}")

    @app_commands.command(name="stop", description="Stop the playing song and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        # Check for excluded user
        if interaction.user.id == self.special_user_id:
            await interaction.response.send_message("LECK EI!")
            return

        player = self.get_player(interaction.guild.id)
        
        if interaction.guild.voice_client:
            await player.stop()
            
            responses = [
                "Das Turnier ist zuende… Mit dem Schläger hab ich seit 1987 nicht mehr verloren",
                "Musik aus! Und Sie gehen jetzt nach vorne und rechnen das nochmal vor",
                "So, Feierabend!"
            ]
            
            await interaction.response.send_message(responses[next(self.stop_cycle)])
        else:
            await interaction.response.send_message("Bot is not connected to any voice channel.")

    @app_commands.command(name="hello", description="Say hello... but not to the bot")
    async def hello(self, interaction: discord.Interaction, message: str, user: discord.User = None, user_id: str = None):
        await interaction.response.defer(ephemeral=True)
        
        target_user = user
        
        if target_user is None and user_id is not None:
            try:
                # Try to convert string to int
                uid = int(user_id)
                target_user = await self.bot.fetch_user(uid)
            except ValueError:
                 await interaction.followup.send("Invalid User ID format.")
                 return
            except discord.NotFound:
                 await interaction.followup.send("User not found with that ID.")
                 return
            except discord.HTTPException as e:
                 await interaction.followup.send(f"Failed to fetch user: {e}")
                 return

        if target_user is None:
             await interaction.followup.send("Please provide either a User or a User ID.")
             return

        try:
            await target_user.send(message)
            await interaction.followup.send(f"Send DM to {target_user.name}: \"{message}\"")
        except discord.Forbidden:
             await interaction.followup.send("Failed to send DM: I don't have permission to message this user.")
        except discord.NotFound: # Should be caught by fetch_user but good to keep for send
             await interaction.followup.send("Failed to send DM: User not found.")
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to send DM: {e}")

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
    bot = MusicBot()
    bot.run(os.getenv('DISCORD_TOKEN'))
