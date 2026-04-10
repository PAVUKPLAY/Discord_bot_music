import os
import base64
import discord
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput
import yt_dlp as youtube_dl
import asyncio
from collections import deque

# ------------------- ПРОВЕРКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ -------------------
required_env_vars = ["DISCORD_TOKEN", "ALLOWED_CHANNEL_ID"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")

# ------------------- РАБОТА С COOKIES (через base64) -------------------
cookies_b64 = os.getenv("YOUTUBE_COOKIES_BASE64")
if cookies_b64:
    try:
        with open("cookies.txt", "wb") as f:
            f.write(base64.b64decode(cookies_b64))
        print("✅ Cookies успешно загружены из переменной окружения.")
    except Exception as e:
        print(f"⚠️ Ошибка при декодировании cookies: {e}")
        # не прерываем запуск, но бот может не работать
else:
    print("⚠️ Переменная YOUTUBE_COOKIES_BASE64 не задана. Бот может столкнуться с ошибками YouTube rate-limit.")

# ------------------- НАСТРОЙКИ -------------------
TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "600"))

# Роли для команды !restart (список ID через запятую)
roles_env = os.getenv("ALLOWED_ROLE_IDS", "")
ALLOWED_ROLE_IDS = [int(role_id.strip()) for role_id in roles_env.split(",") if role_id.strip()]

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'sleep_interval': 5,
    'sleep_interval_requests': 1,
    'extractor_retries': 3,
    'cookiefile': 'cookies.txt',   # используем файл cookies (если существует)
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
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

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
            channel = bot_instance.get_channel(ALLOWED_CHANNEL_ID)
            if channel:
                await channel.send("🔇 Бот отключён из-за 10 минут бездействия.", delete_after=10)

    async def play_next(self, interaction_or_ctx):
        self._reset_idle_timer()
        if not self.queue:
            self.is_playing = False
            self.current = None
            await self.update_panel(interaction_or_ctx, "⏹ Очередь пуста. Добавьте треки кнопкой 'Добавить музыку'.")
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
            await self.send_error(interaction_or_ctx, f"Ошибка получения аудио: {e}")
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
            await self.send_error(interaction_or_ctx, "Прямые ссылки не поддерживаются. Введите название и исполнителя.")
            return False

        search_query = f"ytsearch:{query}"
        loop = asyncio.get_event_loop()
        try:
            with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(search_query, download=False))
        except Exception as e:
            await self.send_error(interaction_or_ctx, f"Не удалось найти трек: {e}")
            return False

        if 'entries' in info and info['entries']:
            entry = info['entries'][0]
            video_url = entry.get('webpage_url')
            if not video_url and entry.get('id'):
                video_url = f"https://youtu.be/{entry['id']}"
            title = entry.get('title', 'Без названия')
            thumbnail = entry.get('thumbnail', '')
            duration = entry.get('duration', 0)
            uploader = entry.get('uploader', 'Неизвестный автор')

            song_data = {
                'url': video_url,
                'title': title,
                'thumbnail': thumbnail,
                'duration': duration,
                'uploader': uploader,
                'query': query
            }
            self.queue.append(song_data)
            await self.send_success(interaction_or_ctx, f"Добавлено в очередь: **{title}**")
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
            await interaction.response.send_message("⏸ Пауза", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Невозможно поставить на паузу", ephemeral=True)

    async def resume(self, interaction):
        if self.voice_client and self.is_paused:
            self.voice_client.resume()
            self.is_paused = False
            self._reset_idle_timer()
            await self.update_panel(interaction, "▶ Воспроизведение продолжено")
            await interaction.response.send_message("▶ Возобновлено", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Невозможно возобновить", ephemeral=True)

    async def skip(self, interaction):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            self._reset_idle_timer()
            await interaction.response.send_message("⏩ Трек пропущен", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Сейчас ничего не играет", ephemeral=True)

    async def show_queue(self, interaction):
        if not self.queue:
            await interaction.response.send_message("📭 Очередь пуста.", ephemeral=True)
            return
        queue_list = [f"{i+1}. {song['title']} ({format_duration(song['duration'])})" for i, song in enumerate(self.queue)]
        text = "**📋 Очередь воспроизведения:**\n" + "\n".join(queue_list[:10])
        if len(queue_list) > 10:
            text += f"\n*... и ещё {len(queue_list)-10} треков*"
        await interaction.response.send_message(text, ephemeral=True)

    async def update_panel(self, interaction_or_ctx, status_text=None):
        if self.guild_id not in player_panels:
            return

        embed = discord.Embed(
            title="🎵 **Музыкальный плеер**",
            color=discord.Color.purple()
        )

        if self.current:
            title = self.current['title']
            uploader = self.current.get('uploader', 'Неизвестный автор')
            duration = format_duration(self.current.get('duration', 0))
            thumbnail = self.current.get('thumbnail', '')

            embed.add_field(
                name="🎶 **Сейчас играет**",
                value=f"**[{title}]({self.current['url']})**\n👤 {uploader}\n⏱ {duration}",
                inline=False
            )
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)

            if self.is_paused:
                status_emoji = "⏸"
                status_text_status = "Приостановлено"
                embed.color = discord.Color.orange()
            elif self.is_playing:
                status_emoji = "▶"
                status_text_status = "Воспроизводится"
                embed.color = discord.Color.green()
            else:
                status_emoji = "⏹"
                status_text_status = "Остановлен"
                embed.color = discord.Color.red()

            embed.add_field(name=f"{status_emoji} **Статус**", value=status_text_status, inline=True)
        else:
            embed.add_field(name="🎶 **Сейчас играет**", value="*Ничего не играет*", inline=False)
            embed.add_field(name="⏹ **Статус**", value="Остановлен", inline=True)

        queue_count = len(self.queue)
        embed.add_field(name="📋 **В очереди**", value=str(queue_count), inline=True)

        if queue_count > 0:
            next_song = self.queue[0]
            embed.add_field(
                name="🎧 **Следующий трек**",
                value=f"**{next_song['title']}**\n⏱ {format_duration(next_song.get('duration', 0))}",
                inline=False
            )

        footer_text = "Используйте кнопки для управления"
        if status_text:
            footer_text = f"{status_text} • {footer_text}"
        embed.set_footer(text=footer_text, icon_url=bot_instance.user.avatar.url if bot_instance.user.avatar else None)

        view = PlayerControlView(self.guild_id)
        message = player_panels[self.guild_id]
        await message.edit(embed=embed, view=view)

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

# ------------------- КНОПКИ ПЛЕЕРА (БЕЗ КНОПКИ ОСТАНОВКИ) -------------------
class PlayerControlView(View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    async def get_player(self, interaction):
        if self.guild_id not in queues:
            await interaction.response.send_message("❌ Плеер не активен. Нажмите 'Подключиться' заново.", ephemeral=True)
            return None
        return queues[self.guild_id]

    @discord.ui.button(label="⏯ Пауза/Возобновить", style=discord.ButtonStyle.secondary, emoji="⏯️")
    async def pause_resume_button(self, interaction: discord.Interaction, button: Button):
        player = await self.get_player(interaction)
        if not player:
            return
        if player.is_paused:
            await player.resume(interaction)
        else:
            await player.pause(interaction)

    @discord.ui.button(label="Пропустить", style=discord.ButtonStyle.primary, emoji="⏩")
    async def skip_button(self, interaction: discord.Interaction, button: Button):
        player = await self.get_player(interaction)
        if not player:
            return
        await player.skip(interaction)

    @discord.ui.button(label="Очередь", style=discord.ButtonStyle.secondary, emoji="📋")
    async def queue_button(self, interaction: discord.Interaction, button: Button):
        player = await self.get_player(interaction)
        if not player:
            return
        await player.show_queue(interaction)

    @discord.ui.button(label="Добавить музыку", style=discord.ButtonStyle.success, emoji="➕")
    async def add_music_button(self, interaction: discord.Interaction, button: Button):
        modal = AddMusicModal(self.guild_id)
        await interaction.response.send_modal(modal)

# ------------------- МОДАЛЬНОЕ ОКНО ДОБАВЛЕНИЯ МУЗЫКИ -------------------
class AddMusicModal(Modal):
    def __init__(self, guild_id):
        super().__init__(title="🎶 Добавить музыку")
        self.guild_id = guild_id
        self.query_input = TextInput(
            label="Название трека или исполнитель",
            placeholder="Пример: Imagine Dragons Believer",
            required=True,
            style=discord.TextStyle.short
        )
        self.add_item(self.query_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.guild_id not in queues:
            await interaction.followup.send("❌ Бот не в голосовом канале. Сначала подключитесь.", ephemeral=True)
            return
        player = queues[self.guild_id]
        query = self.query_input.value
        await player.add_song(interaction, query)

# ------------------- КНОПКИ ПОДКЛЮЧЕНИЯ -------------------
class ConnectView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Подключиться к голосовому каналу", style=discord.ButtonStyle.green, emoji="🔊")
    async def connect_button(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.voice:
            await interaction.response.send_message("❌ Вы не находитесь в голосовом канале.", ephemeral=True)
            return

        guild = interaction.guild
        channel = interaction.user.voice.channel

        if guild.id in queues and queues[guild.id].voice_client and queues[guild.id].voice_client.is_connected():
            await interaction.response.send_message("✅ Бот уже в голосовом канале.", ephemeral=True)
            return

        try:
            voice_client = await channel.connect()
        except Exception as e:
            await interaction.response.send_message(f"❌ Не удалось подключиться: {e}", ephemeral=True)
            return

        player = MusicPlayer(guild.id, voice_client)
        queues[guild.id] = player

        await interaction.response.send_message(f"✅ Подключён к {channel.mention}. Создаю плеер...", ephemeral=True)

        embed = discord.Embed(
            title="🎵 **Музыкальный плеер**",
            description="*Начните добавлять музыку кнопкой ниже!*",
            color=discord.Color.purple()
        )
        embed.add_field(name="🎶 Сейчас играет", value="*Ничего*", inline=True)
        embed.add_field(name="📋 В очереди", value="0", inline=True)
        embed.add_field(name="⏹ Статус", value="Остановлен", inline=True)
        embed.set_footer(text="Используйте кнопки для управления")
        view = PlayerControlView(guild.id)
        message = await interaction.channel.send(embed=embed, view=view)
        player_panels[guild.id] = message

    @discord.ui.button(label="Отключиться", style=discord.ButtonStyle.red, emoji="🔇")
    async def disconnect_button(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        if guild.id not in queues:
            await interaction.response.send_message("❌ Бот не в голосовом канале.", ephemeral=True)
            return
        player = queues[guild.id]
        if player.idle_task and not player.idle_task.done():
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
        await interaction.response.send_message("👋 Бот отключён от голосового канала.", ephemeral=True)

# ------------------- БОТ -------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    global bot_instance
    bot_instance = bot
    print(f"✅ Бот {bot.user} запущен!")

    channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    if channel is None:
        print(f"❌ Ошибка: канал с ID {ALLOWED_CHANNEL_ID} не найден.")
        return

    async for msg in channel.history(limit=30):
        if msg.author == bot.user:
            await msg.delete()

    embed = discord.Embed(
        title="🎧 **Музыкальный бот**",
        description=(
            "Нажмите **Подключиться**, чтобы бот зашёл в ваш голосовой канал и создал плеер.\n\n"
            "🎵 **Как использовать:**\n"
            "• Добавляйте треки через кнопку ➕ **Добавить музыку** (только по названию и исполнителю).\n"
            "• Управляйте воспроизведением кнопками под плеером.\n"
            "• Бот отключится автоматически через 10 минут бездействия.\n\n"
            "*Пример: `Imagine Dragons Believer`*"
        ),
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
    embed.set_footer(text="Музыкальный бот | Версия 2.0")
    view = ConnectView()
    await channel.send(embed=embed, view=view)
    print(f"📢 Панель подключения отправлена в канал {channel.name}")

@bot.command(name='create_panel')
@commands.has_permissions(administrator=True)
async def create_panel(ctx):
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        await ctx.send("❌ Неверный канал.", delete_after=3)
        return
    async for msg in ctx.channel.history(limit=30):
        if msg.author == bot.user:
            await msg.delete()
    embed = discord.Embed(
        title="🎧 **Музыкальный бот**",
        description=(
            "Нажмите **Подключиться**, чтобы бот зашёл в ваш голосовой канал и создал плеер.\n\n"
            "🎵 **Как использовать:**\n"
            "• Добавляйте треки через кнопку ➕ **Добавить музыку** (только по названию и исполнителю).\n"
            "• Управляйте воспроизведением кнопками под плеером.\n"
            "• Бот отключится автоматически через 10 минут бездействия."
        ),
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
    view = ConnectView()
    await ctx.send(embed=embed, view=view)
    await ctx.message.delete()

# ------------------- КОМАНДА RESTART (ТОЛЬКО ДЛЯ РОЛЕЙ) -------------------
@bot.command(name='restart')
async def restart_bot(ctx):
    # Проверка прав
    if not ctx.author.guild_permissions.administrator:
        if not ALLOWED_ROLE_IDS:
            await ctx.send("❌ Команда !restart разрешена только администраторам.", ephemeral=True)
            return
        if not any(role.id in ALLOWED_ROLE_IDS for role in ctx.author.roles):
            await ctx.send("❌ У вас нет прав на использование этой команды.", ephemeral=True)
            return

    # Очистка
    for guild_id, player in list(queues.items()):
        try:
            if player.voice_client and player.voice_client.is_connected():
                await player.voice_client.disconnect()
        except:
            pass
        if player.idle_task and not player.idle_task.done():
            player.idle_task.cancel()
        if guild_id in player_panels:
            try:
                await player_panels[guild_id].delete()
            except:
                pass
    queues.clear()
    player_panels.clear()

    channel = bot.get_channel(ALLOWED_CHANNEL_ID)
    if channel:
        async for msg in channel.history(limit=30):
            if msg.author == bot.user:
                await msg.delete()
        embed = discord.Embed(
            title="🎧 **Музыкальный бот**",
            description=(
                "Нажмите **Подключиться**, чтобы бот зашёл в ваш голосовой канал и создал плеер.\n\n"
                "🎵 **Как использовать:**\n"
                "• Добавляйте треки через кнопку ➕ **Добавить музыку** (только по названию и исполнителю).\n"
                "• Управляйте воспроизведением кнопками под плеером.\n"
                "• Бот отключится автоматически через 10 минут бездействия."
            ),
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
        view = ConnectView()
        await channel.send(embed=embed, view=view)

    await ctx.send("✅ Бот перезапущен. Все очереди очищены.", ephemeral=True)

@restart_bot.error
async def restart_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Недостаточно прав.", ephemeral=True)
    else:
        await ctx.send(f"❌ Ошибка: {error}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"❌ Ошибка: {error}")

# ------------------- ЗАПУСК -------------------
if __name__ == "__main__":
    bot.run(TOKEN)
