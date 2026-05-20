import os
import discord
import requests
import json
from discord.ext import tasks, commands
from discord import app_commands
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DEBUG_CHANNEL_ID = int(os.getenv("DEBUG_CHANNEL_ID")) if os.getenv("DEBUG_CHANNEL_ID") else None
ALS_API_KEY = os.getenv("ALS_API_KEY")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
MY_USER_ID = int(os.getenv("MY_USER_ID")) if os.getenv("MY_USER_ID") else None

DATA_FILE = "channels.json"

# デバッグメッセージを送信するヘルパー関数
async def send_debug(bot, message):
    if DEBUG_MODE and DEBUG_CHANNEL_ID:
        channel = bot.get_channel(DEBUG_CHANNEL_ID)
        if channel:
            # 2000文字制限に配慮し、長い場合はカット
            await channel.send(f"**[DEBUG]** {message[:1900]}", silent=True)

# 権限チェック用の関数
def is_admin_or_me():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator or interaction.user.id == MY_USER_ID:
            return True
        await interaction.response.send_message("このコマンドを実行する権限がありません。", ephemeral=True)
        return False
    return app_commands.check(predicate)

class ApexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)
        
        self.last_br_map = None
        self.last_ranked_map = None
        self.config = self.load_channels()

    # 設定ファイルを読み込む
    def load_channels(self):
        default_config = {"br": [], "ranked": [], "guild_nicks": {}}
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r") as f:
                    data = json.load(f)
                    for key in default_config:
                        if key not in data:
                            data[key] = default_config[key]
                    return data
            except Exception as e:
                print(f"Load error: {e}")
        return default_config

    # 設定ファイルを保存する
    def save_channels(self):
        with open(DATA_FILE, "w") as f:
            json.dump(self.config, f)

    # 起動時のセットアップ
    async def setup_hook(self):
        await self.tree.sync()
        self.map_monitor.start()

    # ニックネームを更新
    async def update_nicknames(self, br_map, rk_map):
        for guild in self.guilds:
            mode = self.config["guild_nicks"].get(str(guild.id), "ranked")
            new_nick = f"BR: {br_map}" if mode == "br" else f"Rank: {rk_map}"
            try:
                if guild.me.display_name != new_nick:
                    await guild.me.edit(nick=new_nick)
                    await send_debug(self, f"Nickname updated in {guild.name}: {new_nick}")
            except Exception as e:
                # ニックネーム更新失敗もデバッグ送信
                await send_debug(self, f"Nickname update failed in {guild.name}: {e}")
                continue

    # メインの監視ループ
    @tasks.loop(seconds=60)
    async def map_monitor(self):
        # 通知先が一つもなく、デバッグモードでもなければ何もしない
        if not self.config["br"] and not self.config["ranked"] and not DEBUG_MODE:
            await send_debug(self, "Monitor loop skipped: No notification channels configured and DEBUG_MODE is off")
            return

        url = f"https://api.mozambiquehe.re/maprotation?version=2&auth={ALS_API_KEY}"
        try:
            # タイムアウトを設定してリクエスト
            response = requests.get(url, timeout=10)
            data = response.json()

            if DEBUG_MODE:
                await send_debug(self, f"API response received. BR channels: {len(self.config['br'])}, Ranked channels: {len(self.config['ranked'])}")

            br_curr = data.get("battle_royale", {}).get("current", {}).get("map")
            rk_curr = data.get("ranked", {}).get("current", {}).get("map")

            # 1. APIから正しいデータが取れていない場合は処理をスキップ（Noneバグ対策）
            if br_curr is None or rk_curr is None:
                await send_debug(self, f"Warning: API returned None (BR: {br_curr}, Rank: {rk_curr}). Skipping.")
                return

            # 2. 初回起動時の処理（変数がNoneの時だけ実行）
            if self.last_br_map is None and self.last_ranked_map is None:
                self.last_br_map = br_curr
                self.last_ranked_map = rk_curr
                await self.update_nicknames(br_curr, rk_curr)
                await send_debug(self, f"Bot started. Current maps: Casual={br_curr}, Ranked={rk_curr}")
                return # 初回は「変更」ではないのでここで終了

            # 3. 変更検知ロジック
            change_detected = False

            # カジュアルの変更チェック
            if br_curr != self.last_br_map:
                await send_debug(self, f"BR Map Change: {self.last_br_map} -> {br_curr}")
                notification_sent = await self.broadcast_map_update("br", f"**カジュアル** のマップが **{br_curr}** に変更されました。")
                await send_debug(self, f"BR notification sent to {notification_sent} channels")
                self.last_br_map = br_curr # ここで値を更新
                change_detected = True

            # ランクの変更チェック
            if rk_curr != self.last_ranked_map:
                await send_debug(self, f"Rank Map Change: {self.last_ranked_map} -> {rk_curr}")
                notification_sent = await self.broadcast_map_update("ranked", f"**ランク** のマップが **{rk_curr}** に変更されました。")
                await send_debug(self, f"Rank notification sent to {notification_sent} channels")
                self.last_ranked_map = rk_curr # ここで値を更新
                change_detected = True

            # 変更があった場合のみニックネームを更新
            if change_detected:
                await self.update_nicknames(br_curr, rk_curr)
            else:
                if DEBUG_MODE:
                    await send_debug(self, f"No map changes detected. BR={br_curr}, Rank={rk_curr}")

        except Exception as e:
            await send_debug(self, f"Monitor Loop Error: {e}")

    # 通知の一斉送信
    async def broadcast_map_update(self, mode_key, message):
        sent_count = 0
        for cid in self.config[mode_key][:]:
            channel = self.get_channel(cid)
            if channel:
                try:
                    await channel.send(message, silent=True)
                    sent_count += 1
                    await send_debug(self, f"Message sent to channel {cid} ({channel.name})")
                except discord.Forbidden:
                    await send_debug(self, f"Permission Denied: Cannot send message to channel {cid}")
                except Exception as e:
                    await send_debug(self, f"Failed to send to channel {cid}: {e}")
            else:
                await send_debug(self, f"Removing invalid channel ID from config: {cid}")
                self.config[mode_key].remove(cid)
                self.save_channels()
        return sent_count

    @map_monitor.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        await send_debug(self, f"Bot is ready! Logged in as {self.user}")

bot = ApexBot()

# --- スラッシュコマンド ---

class MapRote(app_commands.Group):
    @app_commands.command(name="enable", description="通知を有効にします")
    @app_commands.choices(mode=[
        app_commands.Choice(name="カジュアル", value="br"),
        app_commands.Choice(name="ランク", value="ranked")
    ])
    @is_admin_or_me()
    async def enable(self, interaction: discord.Interaction, mode: str):
        if interaction.channel_id not in bot.config[mode]:
            bot.config[mode].append(interaction.channel_id)
            bot.save_channels()
            await interaction.response.send_message(f"通知を有効にしました。", ephemeral=False)
            await send_debug(bot, f"Notification ENABLED for {mode} in channel {interaction.channel_id} (User: {interaction.user})")
        else:
            await interaction.response.send_message("既に有効です。", ephemeral=True)

    @app_commands.command(name="disable", description="通知を無効にします")
    @app_commands.choices(mode=[
        app_commands.Choice(name="カジュアル", value="br"),
        app_commands.Choice(name="ランク", value="ranked")
    ])
    @is_admin_or_me()
    async def disable(self, interaction: discord.Interaction, mode: str):
        if interaction.channel_id in bot.config[mode]:
            bot.config[mode].remove(interaction.channel_id)
            bot.save_channels()
            await interaction.response.send_message(f"通知を無効にしました。", ephemeral=False)
            await send_debug(bot, f"Notification DISABLED for {mode} in channel {interaction.channel_id} (User: {interaction.user})")
        else:
            await interaction.response.send_message("設定されていません。", ephemeral=True)

    @app_commands.command(name="set-nick", description="Botのニックネーム表示モードを設定します")
    @app_commands.choices(mode=[
        app_commands.Choice(name="カジュアルを表示", value="br"),
        app_commands.Choice(name="ランクを表示", value="ranked")
    ])
    @is_admin_or_me()
    async def set_nick(self, interaction: discord.Interaction, mode: str):
        bot.config["guild_nicks"][str(interaction.guild_id)] = mode
        bot.save_channels()
        
        current_br = bot.last_br_map or "取得中..."
        current_rk = bot.last_ranked_map or "取得中..."
        new_nick = f"BR: {current_br}" if mode == "br" else f"Rank: {current_rk}"
        
        try:
            await interaction.guild.me.edit(nick=new_nick)
            await interaction.response.send_message(f"表示モードを **{mode}** に変更しました。", ephemeral=False)
            await send_debug(bot, f"Nickname mode changed to {mode} in guild {interaction.guild.name}")
        except discord.Forbidden:
            await interaction.response.send_message("権限不足でニックネームを変更できませんでした。", ephemeral=False)
            await send_debug(bot, f"Failed to change nick in {interaction.guild.name} due to missing permissions.")

bot.tree.add_command(MapRote(name="map-rote"))
bot.run(TOKEN)