import os
import base64
import discord
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput
import yt_dlp as youtube_dl
import asyncio
from collections import deque

# ------------------- ДИАГНОСТИКА -------------------
print("=== Запуск бота ===")
print(f"Текущая директория: {os.getcwd()}")
print("Список файлов:", os.listdir("."))

# ------------------- ЗАГРУЗКА COOKIES ИЗ ПЕРЕМЕННОЙ ОКРУЖЕНИЯ -------------------
cookies_b64 = os.getenv("YOUTUBE_COOKIES_BASE64")
if cookies_b64:
    try:
        with open("cookies.txt", "wb") as f:
            f.write(base64.b64decode(cookies_b64))
        print(f"✅ Cookies загружены, размер файла: {os.path.getsize('cookies.txt')} байт")
    except Exception as e:
        print(f"❌ Ошибка при создании cookies.txt: {e}")
else:
    print("⚠️ Переменная YOUTUBE_COOKIES_BASE64 не задана")

# ------------------- ПРОВЕРКА ОБЯЗАТЕЛЬНЫХ ПЕРЕМЕННЫХ -------------------
required_env_vars = ["DISCORD_TOKEN", "ALLOWED_CHANNEL_ID"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Отсутствуют: {', '.join(missing_vars)}")

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "600"))

# Роли для !restart (через запятую)
roles_env = os.getenv("ALLOWED_ROLE_IDS", "")
ALLOWED_ROLE_IDS = [int(r.strip()) for r in roles_env.split(",") if r.strip()]

# ------------------- НАСТРОЙКИ YT-DLP С COOKIES -------------------
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'sleep_interval': 5,
    'sleep_interval_requests': 1,
    'extractor_retries': 3,
    'cookiefile': 'cookies.txt',          # используем cookies, если файл есть
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],  # имитация разных клиентов
            'skip': ['hls', 'dash']               # ускоряем извлечение
        }
    }
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -filter:a "volume=0.25"',
}

queues = {}
player_panels = {}
bot_instance = None

# ------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -------------------
def format_duration(seconds):
    if not seconds:
        return "Live"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ------------------- МУЗЫКАЛЬНЫЙ ПЛЕЕР -------------------
class MusicPlayer:
    def __init__(self, guild_id, voice_client):
        self.guild_id = guild_id
        self.voice_client = voice_client
        self.queue = deque()
        self.current = None
        self.is_playing = False
        self.is_paused = False
        self.idle_task = None
        self._reset_idle_timer()

    def _reset_idle_timer(self):
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = asyncio.create_task(self._idle_disconnect())

    async def _idle_disconnect(self):
        await asyncio.sleep(IDLE_TIMEOUT)
        if not self.is_playing and not self.queue:
            if self.voice_client and self.voice_client.is_connected():
                await self.voice_client.disconnect()
            if self.guild_id in player_panels:
                try:
                    await player_panels[self.guild_id].delete()
                except:
                    pass
                del player_panels[self.guild_id]
            if self.guild_id in queues:
                del queues[self.guild_id]
            ch = bot_instance.get_channel(ALLOWED_CHANNEL_ID)
            if ch:
                await ch.send("🔇 Отключён за бездействие (10 мин).", delete_after=10)

    async def play_next(self, interaction_or_ctx):
        self._reset_idle_timer()
        if not self.queue:
            self.is_playing = False
            self.current = None
            await self.update_panel(interaction_or_ctx, "Очередь пуста")
            return

        self.current = self.queue.popleft()
        self.is_playing = True
        self.is_paused = False

        loop = asyncio.get_event_loop()
        try:
            with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(self.current['url'], download=False))
                audio_url = info['url']
        except Exception as e:
            await self.send_error(interaction_or_ctx, f"Ошибка аудио: {e}")
            await self.play_next(interaction_or_ctx)
            return

        def after_play(error):
            if error:
                asyncio.run_coroutine_threadsafe(
                    self.send_error(interaction_or_ctx, f"Ошибка воспроизведения: {error}"),
                    bot_instance.loop
                )
            asyncio.run_coroutine_threadsafe(self.play_next(interaction_or_ctx), bot_instance.loop)

        self.voice_client.play(discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS), after=after_play)
        await self.update_panel(interaction_or_ctx)

    async def add_song(self, interaction_or_ctx, query):
        self._reset_idle_timer()
        if query.startswith(('http://', 'https://', 'www.', 'youtu.be', 'youtube.com')):
            await self.send_error(interaction_or_ctx, "Ссылки запрещены. Введите название и исполнителя.")
            return False

        search_query = f"ytsearch:{query}"
        loop = asyncio.get_event_loop()
        try:
            with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(search_query, download=False))
        except Exception as e:
            await self.send_error(interaction_or_ctx, f"Ошибка поиска: {e}")
            return False

        if 'entries' in info and info['entries']:
            entry = info['entries'][0]
            url = entry.get('webpage_url') or f"https://youtu.be/{entry['id']}"
            song = {
                'url': url,
                'title': entry.get('title', 'Без названия'),
                'thumbnail': entry.get('thumbnail', ''),
                'duration': entry.get('duration', 0),
                'uploader': entry.get('uploader', 'Неизвестен')
            }
            self.queue.append(song)
            await self.send_success(interaction_or_ctx, f"Добавлено: **{song['title']}**")
        else:
            await self.send_error(interaction_or_ctx, "Ничего не найдено.")
            return False

        if not self.is_playing:
            await self.play_next(interaction_or_ctx)
        else:
            await self.update_panel(interaction_or_ctx)
        return True

    async def pause(self, interaction):
        if self.voice_client and self.voice_client.is_playing() and not self.is_paused:
            self.voice_client.pause()
            self.is_paused = True
            self._reset_idle_timer()
            await self.update_panel(interaction, "⏸ Пауза")
            await interaction.response.send_message("Пауза", ephemeral=True)
        else:
            await interaction.response.send_message("Нельзя поставить на паузу", ephemeral=True)

    async def resume(self, interaction):
        if self.voice_client and self.is_paused:
            self.voice_client.resume()
            self.is_paused = False
            self._reset_idle_timer()
            await self.update_panel(interaction, "▶ Продолжено")
            await interaction.response.send_message("Продолжено", ephemeral=True)
        else:
            await interaction.response.send_message("Нельзя возобновить", ephemeral=True)

    async def skip(self, interaction):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            self._reset_idle_timer()
            await interaction.response.send_message("⏩ Пропущено", ephemeral=True)
        else:
            await interaction.response.send_message("Ничего не играет", ephemeral=True)

    async def show_queue(self, interaction):
        if not self.queue:
            await interaction.response.send_message("Очередь пуста", ephemeral=True)
            return
        lines = [f"{i+1}. {s['title']} ({format_duration(s['duration'])})" for i, s in enumerate(self.queue)]
        text = "**Очередь:**\n" + "\n".join(lines[:10])
        if len(lines) > 10:
            text += f"\n*... ещё {len(lines)-10}*"
        await interaction.response.send_message(text, ephemeral=True)

    async def update_panel(self, interaction_or_ctx, status_text=None):
        if self.guild_id not in player_panels:
            return
        embed = discord.Embed(title="🎵 Музыкальный плеер", color=discord.Color.purple())
        if self.current:
            embed.add_field(
                name="🎶 Сейчас играет",
                value=f"**[{self.current['title']}]({self.current['url']})**\n👤 {self.current['uploader']}\n⏱ {format_duration(self.current['duration'])}",
                inline=False
            )
            if self.current.get('thumbnail'):
                embed.set_thumbnail(url=self.current['thumbnail'])
            status = "▶ Играет" if self.is_playing else ("⏸ Пауза" if self.is_paused else "⏹ Остановлен")
            embed.color = discord.Color.green() if self.is_playing else (discord.Color.orange() if self.is_paused else discord.Color.red())
            embed.add_field(name="Статус", value=status, inline=True)
        else:
            embed.add_field(name="🎶 Сейчас играет", value="*Ничего*", inline=False)
            embed.add_field(name="Статус", value="Остановлен", inline=True)
        embed.add_field(name="📋 В очереди", value=str(len(self.queue)), inline=True)
        if self.queue:
            nxt = self.queue[0]
            embed.add_field(name="🎧 Следующий", value=f"**{nxt['title']}**\n⏱ {format_duration(nxt['duration'])}", inline=False)
        embed.set_footer(text="Кнопки управления" + (f" • {status_text}" if status_text else ""))
        view = PlayerControlView(self.guild_id)
        msg = player_panels[self.guild_id]
        await msg.edit(embed=embed, view=view)

    async def send_success(self, interaction_or_ctx, text):
        if isinstance(interaction_or_ctx, discord.Interaction):
            await interaction_or_ctx.followup.send(f"✅ {text}", ephemeral=True)
        else:
            await interaction_or_ctx.send(f"✅ {text}")

    async def send_error(self, interaction_or_ctx, text):
        if isinstance(interaction_or_ctx, discord.Interaction):
            await interaction_or_ctx.followup.send(f"❌ {text}", ephemeral=True)
        else:
            await interaction_or_ctx.send(f"❌ {text}")

# ------------------- КНОПКИ ПЛЕЕРА -------------------
class PlayerControlView(View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    async def get_player(self, interaction):
        if self.guild_id not in queues:
            await interaction.response.send_message("Плеер не активен, нажмите «Подключиться»", ephemeral=True)
            return None
        return queues[self.guild_id]

    @discord.ui.button(label="⏯ Пауза/Возобн.", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: Button):
        player = await self.get_player(interaction)
        if not player:
            return
        if player.is_paused:
            await player.resume(interaction)
        else:
            await player.pause(interaction)

    @discord.ui.button(label="⏩ Пропустить", style=discord.ButtonStyle.primary)
    async def skip_btn(self, interaction: discord.Interaction, button: Button):
        player = await self.get_player(interaction)
        if not player:
            return
        await player.skip(interaction)

    @discord.ui.button(label="📋 Очередь", style=discord.ButtonStyle.secondary)
    async def queue_btn(self, interaction: discord.Interaction, button: Button):
        player = await self.get_player(interaction)
        if not player:
            return
        await player.show_queue(interaction)

    @discord.ui.button(label="➕ Добавить", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: Button):
        modal = AddMusicModal(self.guild_id)
        await interaction.response.send_modal(modal)

# ------------------- МОДАЛЬНОЕ ОКНО -------------------
class AddMusicModal(Modal):
    def __init__(self, guild_id):
        super().__init__(title="Добавить музыку")
        self.guild_id = guild_id
        self.query = TextInput(label="Название или исполнитель", placeholder="Пример: Imagine Dragons Believer", required=True)
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.guild_id not in queues:
            await interaction.followup.send("Бот не в голосовом канале. Сначала подключитесь.", ephemeral=True)
            return
        player = queues[self.guild_id]
        await player.add_song(interaction, self.query.value)

# ------------------- КНОПКИ ПОДКЛЮЧЕНИЯ -------------------
class ConnectView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔊 Подключиться", style=discord.ButtonStyle.green)
    async def connect(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.voice:
            await interaction.response.send_message("Вы не в голосовом канале", ephemeral=True)
            return
        guild = interaction.guild
        channel = interaction.user.voice.channel
        if guild.id in queues and queues[guild.id].voice_client and queues[guild.id].voice_client.is_connected():
            await interaction.response.send_message("Бот уже в голосовом канале", ephemeral=True)
            return
        try:
            vc = await channel.connect()
        except Exception as e:
            await interaction.response.send_message(f"Ошибка подключения: {e}", ephemeral=True)
            return
        player = MusicPlayer(guild.id, vc)
        queues[guild.id] = player
        await interaction.response.send_message(f"✅ Подключён к {channel.mention}", ephemeral=True)
        embed = discord.Embed(title="🎵 Музыкальный плеер", description="*Добавьте музыку кнопкой ниже*", color=discord.Color.purple())
        embed.add_field(name="Сейчас играет", value="*Ничего*", inline=True)
        embed.add_field(name="В очереди", value="0", inline=True)
        embed.add_field(name="Статус", value="Остановлен", inline=True)
        view = PlayerControlView(guild.id)
        msg = await interaction.channel.send(embed=embed, view=view)
        player_panels[guild.id] = msg

    @discord.ui.button(label="🔇 Отключиться", style=discord.ButtonStyle.red)
    async def disconnect(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        if guild.id not in queues:
            await interaction.response.send_message("Бот не в голосовом канале", ephemeral=True)
            return
        player = queues[guild.id]
        if player.idle_task:
            player.idle_task.cancel()
        if player.voice_client:
            await player.voice_client.disconnect()
        if guild.id in player_panels:
            try:
                await player_panels[guild.id].delete()
            except:
                pass
            del player_panels[guild.id]
        del queues[guild.id]
        await interaction.response.send_message("👋 Отключён", ephemeral=True)

# ------------------- БОТ -------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    global bot_instance
    bot_instance = bot
    print(f"✅ Бот {bot.user} запущен")
    channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    if not channel:
        print(f"❌ Канал {ALLOWED_CHANNEL_ID} не найден")
        return
    async for msg in channel.history(limit=30):
        if msg.author == bot.user:
            await msg.delete()
    embed = discord.Embed(title="🎧 Музыкальный бот", description="Нажмите **Подключиться**, чтобы начать.\n\n• Добавляйте треки по названию\n• Автоотключение через 10 мин бездействия\n• Пример: `Imagine Dragons Believer`", color=discord.Color.gold())
    embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
    view = ConnectView()
    await channel.send(embed=embed, view=view)
    print(f"Панель отправлена в {channel.name}")

@bot.command(name='create_panel')
@commands.has_permissions(administrator=True)
async def create_panel(ctx):
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        await ctx.send("Неверный канал", delete_after=3)
        return
    async for msg in ctx.channel.history(limit=30):
        if msg.author == bot.user:
            await msg.delete()
    embed = discord.Embed(title="🎧 Музыкальный бот", description="Нажмите **Подключиться**", color=discord.Color.gold())
    embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
    view = ConnectView()
    await ctx.send(embed=embed, view=view)
    await ctx.message.delete()

@bot.command(name='restart')
async def restart_bot(ctx):
    if not ctx.author.guild_permissions.administrator and not any(r.id in ALLOWED_ROLE_IDS for r in ctx.author.roles):
        await ctx.send("❌ Недостаточно прав", ephemeral=True)
        return
    for gid, pl in list(queues.items()):
        try:
            if pl.voice_client and pl.voice_client.is_connected():
                await pl.voice_client.disconnect()
        except:
            pass
        if pl.idle_task:
            pl.idle_task.cancel()
        if gid in player_panels:
            try:
                await player_panels[gid].delete()
            except:
                pass
    queues.clear()
    player_panels.clear()
    ch = bot.get_channel(ALLOWED_CHANNEL_ID)
    if ch:
        async for msg in ch.history(limit=30):
            if msg.author == bot.user:
                await msg.delete()
        embed = discord.Embed(title="🎧 Музыкальный бот", description="Нажмите **Подключиться**", color=discord.Color.gold())
        embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
        view = ConnectView()
        await ch.send(embed=embed, view=view)
    await ctx.send("✅ Перезапущено", ephemeral=True)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"❌ {error}")

if __name__ == "__main__":
    bot.run(TOKEN)
