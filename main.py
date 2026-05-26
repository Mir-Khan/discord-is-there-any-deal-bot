import logging
import sys

# Configure logging immediately before any other imports
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s', stream=sys.stdout)
logger = logging.getLogger('itad_bot')
logger.info("Python process started. Initializing imports...")

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import aiosqlite
import os
import time
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DATABASE_PATH = os.getenv('DATABASE_PATH', 'wishlist.db')

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ITAD_API_KEY = os.getenv('ITAD_API_KEY')

CURRENCY_SYMBOLS = {
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
    "CAD": "C$",
    "AUD": "A$",
    "JPY": "¥"
}

class ITADBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True # Useful for DMing users if they are in shared servers
        super().__init__(command_prefix="!", intents=intents)
        self.session = None
        self.db = None

    async def setup_hook(self):
        # Initialize the aiohttp session here for persistence
        self.session = aiohttp.ClientSession()

        # Initialize Database
        self.db = await aiosqlite.connect(DATABASE_PATH)
        # Enable WAL mode for better performance and reliability on network volumes
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                user_id INTEGER,
                game_id TEXT,
                game_title TEXT,
                country TEXT,
                alert_method TEXT DEFAULT 'dm',
                guild_id INTEGER,
                channel_id INTEGER,
                last_deal_url TEXT,
                alert_state INTEGER DEFAULT 0, -- 0: None, 1: First Sent, 2: Expiry Sent
                is_snoozed INTEGER DEFAULT 0,
                platform TEXT DEFAULT 'pc',
                PRIMARY KEY (user_id, game_id)
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                alert_channel_id INTEGER
            )
        """)

        # Migration: Ensure existing tables have new columns if they were created with an older version
        async with self.db.execute("PRAGMA table_info(wishlist)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "last_deal_url" not in columns:
                await self.db.execute("ALTER TABLE wishlist ADD COLUMN last_deal_url TEXT")
            if "alert_state" not in columns:
                await self.db.execute("ALTER TABLE wishlist ADD COLUMN alert_state INTEGER DEFAULT 0")
            if "is_snoozed" not in columns:
                await self.db.execute("ALTER TABLE wishlist ADD COLUMN is_snoozed INTEGER DEFAULT 0")
            if "platform" not in columns:
                await self.db.execute("ALTER TABLE wishlist ADD COLUMN platform TEXT DEFAULT 'pc'")

        await self.db.commit()

        # Start background tasks
        self.check_wishlists.start()

        # Syncs slash commands with Discord.
        logger.info("Syncing slash commands...")
        self.tree.add_command(wishlist_group)
        await self.tree.sync()
        logger.info(f"Slash commands synced for {self.user}")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("------")

    async def on_guild_join(self, guild: discord.Guild):
        """Triggered when the bot joins a new server."""
        logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
        
        # Find the best channel to send the welcome message
        target_channel = guild.system_channel
        if not target_channel or not target_channel.permissions_for(guild.me).send_messages:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    target_channel = channel
                    break
        
        if target_channel:
            embed = discord.Embed(
                title="🎮 Welcome to ITAD Deal Bot!",
                description=(
                    "I'm here to help you and your members find the best game deals!\n\n"
                    "**Core Features:**\n"
                    "• `/deal`: Search for any game's current deals and historical lows.\n"
                    "• `/wishlist add`: Track a game and get notified when it goes on sale.\n"
                    "• **Smart Alerts**: I'll only bug you twice—once when a deal starts, and once before it expires.\n\n"
                    "**Setup:**\n"
                    "Use the menu below to choose a default channel for deal alerts in this server."
                ),
                color=0x2ecc71
            )
            await target_channel.send(embed=embed, view=WelcomeView())

    async def close(self):
        await super().close()
        if self.db:
            await self.db.close()
        if self.session:
            await self.session.close()

    @tasks.loop(hours=6)
    async def check_wishlists(self):
        """Background task to check for deals on wishlisted games."""
        if not self.db or not self.session:
            logger.warning("Skipping wishlist check: DB or session not initialized.")
            return

        logger.info("Running periodic wishlist price check...")
        
        # 1. Fetch all wishlist items once and group them by country and game_id
        all_wishlist_items = {} 
        
        # Ensure the bot's database connection is open before trying to use it
        if not self.db:
            logger.error("Database connection is not established. Cannot run wishlist check.")
            return

        # Fetch all wishlist items
        async with self.db.execute(
            "SELECT user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform FROM wishlist"
        ) as cursor:
            for item in await cursor.fetchall():
                platform, country, game_id = item[10], item[3], item[1]
                all_wishlist_items.setdefault(platform, {}).setdefault(country, {}).setdefault(game_id, []).append(item)

        if not all_wishlist_items:
            logger.info("No items in wishlist. Skipping deal check.")
            return

        now = int(time.time())

        # 1. Process PC Deals (ITAD)
        pc_items = all_wishlist_items.get('pc', {})
        for country, games_in_country in pc_items.items():
            game_ids = list(games_in_country.keys())
            response = await fetch_itad_data(
                self.session, "games/prices/v2", 
                params={"country": country, "nondeals": 0, "vouchers": 1}, 
                method='POST', json_data=game_ids
            )
            if response:
                for game_result in response:
                    await self.process_itad_alert(game_result, games_in_country, now)

        # 2. Process Console Deals (NEXARDA)
        for plat in ['ps4', 'ps5', 'xboxone', 'xboxseries', 'switch']:
            plat_items = all_wishlist_items.get(plat, {})
            for country, games_in_country in plat_items.items():
                for game_id, watchers in games_in_country.items():
                    # NEXARDA prices endpoint
                    price_data = await fetch_nexarda_data(self.session, "prices", {"type": "id", "id": game_id})
                    if price_data and price_data.get('results'):
                        # Get base details for MSRP/Regular price
                        details = await fetch_nexarda_data(self.session, "details", {"type": "id", "id": game_id})
                        await self.process_nexarda_alert(price_data['results'], details, watchers, now)

        logger.info("Wishlist price check completed.")

    async def process_itad_alert(self, game_result, games_in_country, now):
        game_id = game_result.get('id')
        deals = game_result.get('deals', [])
        if not deals: return

        top_deal = deals[0]
        deal_url, expiry = top_deal['url'], top_deal.get('expiry')
        
        # Calculate "Market MSRP" - the lowest regular price across all shops.
        # This prevents alerts for "sales" that are more expensive than regular prices elsewhere.
        market_msrp = min((d['regular']['amount'] for d in deals if d.get('regular')), default=top_deal['regular']['amount'])

        if top_deal['price']['amount'] >= market_msrp:
            logger.info(f"Skipping alert for {game_id}: Sale price ({top_deal['price']['amount']}) is not lower than Market MSRP ({market_msrp}).")
            return

        await self.dispatch_alert(game_id, watchers=games_in_country.get(game_id, []), 
                                  deal_url=deal_url, shop_name=top_deal['shop']['name'], 
                                  price=top_deal['price']['amount'], currency=top_deal['price']['currency'],
                                  cut=top_deal['cut'], expiry=expiry, now=now)

    async def process_nexarda_alert(self, results, details, watchers, now):
        if not results or not watchers: return
        
        # Find the best current price
        best_deal = min(results, key=lambda x: x['price'])
        game_id = watchers[0][1]
        
        # Use base_price from details as regular price if available
        regular_price = details.get('base_price', best_deal['price']) if details else best_deal['price']
        
        if best_deal['price'] >= regular_price:
            return

        cut = int(((regular_price - best_deal['price']) / regular_price) * 100)
        await self.dispatch_alert(game_id, watchers=watchers, 
                                  deal_url=best_deal['shop_url'], shop_name=best_deal['shop_name'], 
                                  price=best_deal['price'], currency='USD',
                                  cut=cut, expiry=None, now=now)

    async def dispatch_alert(self, game_id, watchers, deal_url, shop_name, price, currency, cut, expiry, now):
        title = watchers[0][2]
        dm_alerts, mention_alerts = [], {}

        if expiry is not None:
            try: expiry = int(expiry)
            except: expiry = None

        for watcher in watchers:
            user_id, _, _, _, method, g_id, c_id, last_url, state, snoozed, platform = watcher
            is_new_deal = (deal_url != last_url)
            is_expiring = bool(expiry and (expiry - now) < 28800 and state == 1)

            if not is_new_deal and (snoozed or state == 2 or (state == 1 and not is_expiring)):
                continue

            if method == 'mention' and g_id and c_id:
                mention_alerts.setdefault((g_id, c_id), []).append(user_id)
            else:
                dm_alerts.append(user_id)

            # Update DB state
            new_alert_state = 1 if is_new_deal else (2 if is_expiring else state)
            await self.db.execute(
                "UPDATE wishlist SET last_deal_url = ?, alert_state = ?, is_snoozed = 0 WHERE user_id = ? AND game_id = ?",
                (deal_url, new_alert_state, user_id, game_id)
            )
        await self.db.commit()

        price_fmt = f"{CURRENCY_SYMBOLS.get(currency, '$')}{price:.2f}"
        prefix = "🔔 **New Deal Alert!**" if not is_expiring else "⏳ **Final Call! Deal Expiring Soon:**"
        msg = f"{prefix}\n'{title}' ({platform.upper()}) is currently **{price_fmt}** ({cut}% off) at {shop_name}.\nLink: {deal_url}"
        view = WishlistActionView(game_id, title)

        for u_id in dm_alerts:
            try:
                user = await self.fetch_user(u_id)
                if user: await user.send(msg, view=view)
            except: pass

        for (gid, cid), u_ids in mention_alerts.items():
            try:
                channel = self.get_channel(cid) or await self.fetch_channel(cid)
                if channel:
                    pings = " ".join([f"<@{uid}>" for uid in u_ids])
                    await channel.send(content=f"{pings} {msg}", view=view)
            except: pass

bot = ITADBot()

class WishlistActionView(discord.ui.View):
    """Buttons attached to deal alerts for quick management."""
    def __init__(self, game_id, game_title):
        super().__init__(timeout=None) # Keep buttons active
        self.game_id = game_id
        self.game_title = game_title

    @discord.ui.button(label="Snooze this deal", style=discord.ButtonStyle.secondary, emoji="💤")
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.db.execute(
            "UPDATE wishlist SET is_snoozed = 1 WHERE user_id = ? AND game_id = ?",
            (interaction.user.id, self.game_id)
        )
        await bot.db.commit()
        await interaction.response.send_message(
            f"Snoozed alerts for '{self.game_title}' until a new deal is detected.", ephemeral=True
        )
        button.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Remove from Wishlist", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.db.execute(
            "DELETE FROM wishlist WHERE user_id = ? AND game_id = ?",
            (interaction.user.id, self.game_id)
        )
        await bot.db.commit()
        await interaction.response.send_message(f"Removed '{self.game_title}' from your wishlist.", ephemeral=True)
        self.stop()

class DealView(discord.ui.View):
    """Buttons attached to deal search results for quick wishlist management."""
    def __init__(self, game_id, game_title, country, is_on_wishlist, guild_id=None, top_deal=None, owner_id=None, platform='pc'):
        super().__init__(timeout=120)
        self.game_id = game_id
        self.game_title = game_title
        self.platform = platform
        self.country = country
        self.is_on_wishlist = is_on_wishlist
        self.guild_id = guild_id
        self.top_deal = top_deal
        self.owner_id = owner_id
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and str(interaction.user.id) != str(self.owner_id):
            await interaction.response.send_message("You didn't perform this search. Use `/deal` to manage your own wishlist!", ephemeral=True)
            return False
        return True

    def _update_buttons(self):
        self.clear_items()
        if not self.is_on_wishlist:
            # Add DM button
            btn_dm = discord.ui.Button(label="Add (DM Alert)", style=discord.ButtonStyle.success, emoji="✉️")
            btn_dm.callback = self.add_dm_callback
            self.add_item(btn_dm)

            # Add Mention button (only if in a server)
            if self.guild_id:
                btn_men = discord.ui.Button(label="Add (Mention Alert)", style=discord.ButtonStyle.success, emoji="🔔")
                btn_men.callback = self.add_mention_callback
                self.add_item(btn_men)
        else:
            # Remove button
            btn_rem = discord.ui.Button(label="Remove from Wishlist", style=discord.ButtonStyle.danger, emoji="🗑️")
            btn_rem.callback = self.remove_callback
            self.add_item(btn_rem)

    async def add_dm_callback(self, interaction: discord.Interaction):
        deal_url = self.top_deal['url'] if self.top_deal else None
        expiry = self.top_deal.get('expiry') if self.top_deal else None
        alert_state = 1 if deal_url else 0
        
        is_expiring_soon = False
        if self.top_deal and expiry:
            try:
                if (int(expiry) - int(time.time())) < 28800:
                    alert_state = 2
                    is_expiring_soon = True
            except (ValueError, TypeError): pass

        await bot.db.execute(
            "INSERT OR REPLACE INTO wishlist (user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (interaction.user.id, self.game_id, self.game_title, self.country, 'dm', None, None, deal_url, alert_state, self.platform)
        )
        await bot.db.commit()
        
        if is_expiring_soon:
            price = f"{CURRENCY_SYMBOLS.get(self.top_deal['price']['currency'], '')}{self.top_deal['price']['amount']:.2f}"
            msg = f"⏳ **Final Call! Deal Expiring Soon:**\n'{self.game_title}' is currently **{price}** ({self.top_deal['cut']}% off) at {self.top_deal['shop']['name']}.\nLink: {self.top_deal['url']}"
            try:
                await interaction.user.send(msg, view=WishlistActionView(self.game_id, self.game_title))
            except Exception as e:
                logger.warning(f"Could not send immediate expiry DM: {e}")

        self.is_on_wishlist = True
        self._update_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"✅ Added **{self.game_title}** to your wishlist with DM alerts!", ephemeral=True)

    async def add_mention_callback(self, interaction: discord.Interaction):
        async with bot.db.execute("SELECT alert_channel_id FROM guild_settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
            row = await cursor.fetchone()
            channel_id = row[0] if row else interaction.channel_id

        deal_url = self.top_deal['url'] if self.top_deal else None
        expiry = self.top_deal.get('expiry') if self.top_deal else None
        alert_state = 1 if deal_url else 0
        
        is_expiring_soon = False
        if self.top_deal and expiry:
            try:
                if (int(expiry) - int(time.time())) < 28800:
                    alert_state = 2
                    is_expiring_soon = True
            except (ValueError, TypeError): pass

        await bot.db.execute(
            "INSERT OR REPLACE INTO wishlist (user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (interaction.user.id, self.game_id, self.game_title, self.country, 'mention', interaction.guild_id, channel_id, deal_url, alert_state, self.platform)
        )
        await bot.db.commit()
        
        if is_expiring_soon:
            price = f"{CURRENCY_SYMBOLS.get(self.top_deal['price']['currency'], '')}{self.top_deal['price']['amount']:.2f}"
            msg = f"⏳ **Final Call! Deal Expiring Soon:**\n'{self.game_title}' is currently **{price}** ({self.top_deal['cut']}% off) at {self.top_deal['shop']['name']}.\nLink: {self.top_deal['url']}"
            try:
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                if channel:
                    await channel.send(content=f"<@{interaction.user.id}> {msg}", view=WishlistActionView(self.game_id, self.game_title))
            except Exception as e:
                logger.warning(f"Could not send immediate expiry mention: {e}")

        self.is_on_wishlist = True
        self._update_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"✅ Added **{self.game_title}** to your wishlist! You'll be mentioned in <#{channel_id}>.", ephemeral=True)

    async def remove_callback(self, interaction: discord.Interaction):
        await bot.db.execute("DELETE FROM wishlist WHERE user_id = ? AND game_id = ?", (interaction.user.id, self.game_id))
        await bot.db.commit()
        self.is_on_wishlist = False
        self._update_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"🗑️ Removed **{self.game_title}** from your wishlist.", ephemeral=True)

class WelcomeView(discord.ui.View):
    """A view sent on bot join to highlight features and set a default channel."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select a default alert channel...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need 'Manage Server' permissions to do this!", ephemeral=True)
            
        channel = select.values[0]
        await bot.db.execute(
            "INSERT OR REPLACE INTO guild_settings (guild_id, alert_channel_id) VALUES (?, ?)",
            (interaction.guild_id, channel.id)
        )
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Default alert channel set to {channel.mention}!", ephemeral=True)

async def fetch_itad_data(session, endpoint, params=None, method='GET', json_data=None):
    """Helper to make async requests to ITAD API v2"""
    if params is None:
        params = {}
    params['key'] = ITAD_API_KEY
    async with session.request(method, f"https://api.isthereanydeal.com/{endpoint}", params=params, json=json_data) as resp:
        if resp.status == 200:
            return await resp.json()
        
        error_text = await resp.text()
        logger.error(f"ITAD API Error: {resp.status} on {endpoint}. Method: {method}. Details: {error_text}")

        # Handle unsupported country codes (usually returns 400 Bad Request)
        if resp.status == 400 and ("country" in error_text.lower() or "region" in error_text.lower()):
            raise ValueError(f"The country code '{params.get('country')}' is not supported by IsThereAnyDeal.")
        return None

async def fetch_nexarda_data(session, endpoint, params=None):
    """Helper for NEXARDA API v3 (No API Key required)"""
    if params is None: params = {}
    async with session.get(f"https://www.nexarda.com/api/v3/{endpoint}", params=params) as resp:
        if resp.status == 200:
            return await resp.json()
        logger.error(f"NEXARDA API Error: {resp.status} on {endpoint}")
        return None

class GameSelectView(discord.ui.View):
    """A view containing a dropdown for selecting a game from search results."""
    def __init__(self, games, author, country, action="show", alert_method="dm", platform="pc"):
        super().__init__(timeout=60)
        self.author = author
        self.country = country
        self.action = action # "show" (deals) or "add" (wishlist)
        self.alert_method = alert_method
        self.platform = platform
        self.games = games
        options = [
            discord.SelectOption(label=g['title'], value=g['id'], description=f"ID: {g['id']}")
            for g in games[:25] # Discord limits select menus to 25 options
        ]
        self.select = discord.ui.Select(placeholder="Select a game...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.author.id):
            return await interaction.response.send_message("This isn't your search! Use `/deal` to manage your own wishlist.", ephemeral=True)
        
        game_id = self.select.values[0]
        game_obj = next((g for g in self.games if g['id'] == game_id), None)
        if not game_obj:
            return await interaction.response.send_message("Could not find game details.", ephemeral=True)
            
        game_title = game_obj['title']
        slug = game_obj.get('slug')
        
        if self.action == "add":
            guild_id = interaction.guild_id if self.alert_method == "mention" else None
            channel_id = None
            
            if self.alert_method == "mention":
                # Check if a default server channel is configured
                async with bot.db.execute("SELECT alert_channel_id FROM guild_settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                    row = await cursor.fetchone()
                    channel_id = row[0] if row else interaction.channel_id

            await bot.db.execute(
                "INSERT OR REPLACE INTO wishlist (user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?)",
                (interaction.user.id, game_id, game_title, self.country, self.alert_method, guild_id, channel_id, self.platform)
            )
            await bot.db.commit()
            msg = f"✅ Added **{game_title}** ({self.country}) to your wishlist!"
            msg += " I'll DM you when it goes on sale." if self.alert_method == "dm" else f" I'll mention you in <#{channel_id}> when it goes on sale."
            
            return await interaction.response.edit_message(content=msg, view=None)
        
        await interaction.response.defer()
        await show_deals(interaction, game_id, game_title, self.country, include_wishlist_buttons=True, slug=slug, platform=self.platform)

async def show_deals(interaction, game_id, game_title, country, include_wishlist_buttons=True, slug=None, platform='pc'):
    """Fetches deals and edits the original response with an embed."""
    logger.info(f"Fetching deals for '{game_title}' (ID: {game_id}) in region: {country}. Wishlist buttons: {include_wishlist_buttons}")
    
    # Check if we should redirect the reply to a designated alert channel
    alert_channel = None
    if interaction.guild_id:
        async with bot.db.execute("SELECT alert_channel_id FROM guild_settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] != interaction.channel_id:
                try:
                    # Try to get channel from cache, fallback to fetching
                    alert_channel = bot.get_channel(row[0]) or await bot.fetch_channel(row[0])
                except Exception as e:
                    logger.warning(f"Failed to resolve alert channel {row[0]} for guild {interaction.guild_id}: {e}")

    if platform != 'pc':
        # Logic for NEXARDA Console Deals
        price_data = await fetch_nexarda_data(bot.session, "prices", {"type": "id", "id": game_id})
        details = await fetch_nexarda_data(bot.session, "details", {"type": "id", "id": game_id})
        
        if not price_data or not price_data.get('results'):
            return await interaction.followup.send(f"No current deals found for {game_title} on this platform.")

        results = price_data['results']
        embed = discord.Embed(
            title=f"{game_title} ({platform.upper()})",
            url=f"https://www.nexarda.com/products/{game_id}",
            description=f"📉 **Lowest Price:** ${min(results, key=lambda x: x['price'])['price']:.2f}",
            color=0x3498db,
            timestamp=interaction.created_at
        )
        embed.set_author(name=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="Data provided by NEXARDA")
        
        for deal in results[:10]:
            embed.add_field(
                name=deal['shop_name'],
                value=f"**${deal['price']:.2f}**\n[Link]({deal['shop_url']})",
                inline=True
            )

        view = None
        if include_wishlist_buttons:
            async with bot.db.execute("SELECT 1 FROM wishlist WHERE user_id = ? AND game_id = ? AND platform = ?", (interaction.user.id, game_id, platform)) as cursor:
                is_on_wishlist = await cursor.fetchone() is not None
            view = DealView(game_id, game_title, country, is_on_wishlist, interaction.guild_id, owner_id=interaction.user.id, platform=platform)

        if alert_channel:
            await alert_channel.send(embed=embed, view=view)
            await interaction.edit_original_response(content=f"✅ I've sent the {platform.upper()} deals for **{game_title}** to {alert_channel.mention}!", embed=None, view=None)
        else:
            await interaction.edit_original_response(content=None, embed=embed, view=view)
        return

    # Fetch historical low data from the Overview endpoint
    overview_response = await fetch_itad_data(
        bot.session,
        "games/overview/v2",
        params={"country": country},
        method='POST',
        json_data=[game_id]
    )

    historical_low_str = "N/A"
    bundle_val = "None"
    image_url = None

    if overview_response and isinstance(overview_response.get('prices'), list) and overview_response['prices']:
        game_overview = overview_response['prices'][0]
        logger.info(f"ITAD Overview metadata for image: {game_overview.get('image')}")
        lowest = game_overview.get('lowest')
        if lowest:
            low_price = lowest['price']['amount']
            low_currency_code = lowest['price'].get('currency', 'USD')
            low_currency = CURRENCY_SYMBOLS.get(low_currency_code, low_currency_code)
            low_shop = lowest['shop']['name']
            historical_low_str = f"**{low_currency}{low_price:.2f}** at {low_shop}"

        bundle_count = game_overview.get('bundled', 0)
        if bundle_count > 0:
            active_bundles = overview_response.get('bundles', [])
            if active_bundles:
                bundle_links = [f"[{b['title']}]({b['url']})" for b in active_bundles]
                bundle_val = ", ".join(bundle_links)
            else:
                bundle_val = f"{bundle_count} active"

        # Prioritize image from metadata if available
        if game_overview.get('image'):
            image_url = game_overview['image'].get('banner') or game_overview['image'].get('thumb')

    # Fallback to ITAD's asset CDN if metadata is missing
    if not image_url:
        image_url = f"https://assets.isthereanydeal.com/{game_id}/banner400.jpg"

    if image_url and image_url.startswith('//'):
        image_url = f"https:{image_url}"
    
    logger.info(f"Final image URL used: {image_url}")

    # ITAD API v2 Prices endpoint provides all available deals across different shops.
    response = await fetch_itad_data(
        bot.session, 
        "games/prices/v2", 
        params={"country": country, "nondeals": 1, "vouchers": 1}, 
        method='POST', 
        json_data=[game_id]
    )
    
    if not isinstance(response, list) or not response:
        logger.warning(f"No valid price data found for {game_title} (ID: {game_id}). Response: {response}")
        logger.info(f"Raw Response: {response}")
        return await interaction.followup.send(f"Could not find deal data for {game_title}.")

    # The API returns a list of results. Each result contains a 'deals' list for that game.
    data = response[0]
    deals = data.get('deals', [])
    
    logger.info(f"Found {len(deals)} current deals for {game_title}")
    if not deals:
        logger.warning(f"No current deals found for {game_title} (ID: {game_id}). Response item: {data}")
        return await interaction.followup.send(f"No current deals found for {game_title}.")

    embed = discord.Embed(
        title=game_title,
        url=f"https://isthereanydeal.com/game/{slug or game_id}/info/",
        description=f"📉 **Historical Low:** {historical_low_str}\n🎁 **Bundles:** {bundle_val}",
        color=0x2ecc71, # Vibrant Emerald Green
        timestamp=interaction.created_at
    )
    
    embed.set_author(name=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="Data provided by IsThereAnyDeal", icon_url="https://isthereanydeal.com/favicon.ico")
    embed.set_image(url=image_url)

    embed.add_field(name="\u200b", value="**Top Deals**", inline=False)

    for deal in deals[:10]: # List top 10 deals
        shop_name = deal['shop']['name']
        currency_code = deal['price'].get('currency', 'USD')
        currency_symbol = CURRENCY_SYMBOLS.get(currency_code, currency_code)
        current_price = f"{currency_symbol}{deal['price']['amount']:.2f}"
        
        regular_amount = deal.get('regular', {}).get('amount', deal['price']['amount'])
        regular_price = f"{currency_symbol}{regular_amount:.2f}"
        
        # Added: Platform/DRM info (e.g., Steam, GOG, Epic)
        platform = f" ({deal['drm'][0]['name']})" if deal.get('drm') else ""
        cut = f"{deal['cut']}% off{platform}"
        link = deal['url']
        
        embed.add_field(
            name=shop_name,
            value=f"**{current_price}** (~~{regular_price}~~)\n{cut} · [Link]({link})",
            inline=True
        )

    # Determine if we should add wishlist management buttons
    view = None
    if include_wishlist_buttons:
        async with bot.db.execute("SELECT 1 FROM wishlist WHERE user_id = ? AND game_id = ?", (interaction.user.id, game_id)) as cursor:
            is_on_wishlist = await cursor.fetchone() is not None
        
        view = DealView(game_id, game_title, country, is_on_wishlist, interaction.guild_id, top_deal=deals[0], owner_id=interaction.user.id)

    # Send to alert channel if redirected, otherwise reply in-place
    if alert_channel:
        try:
            await alert_channel.send(embed=embed, view=view)
            await interaction.edit_original_response(content=f"✅ I've sent the deals for **{game_title}** to {alert_channel.mention}!", embed=None, view=None)
            return
        except discord.Forbidden:
            logger.warning(f"Permission denied for alert channel {alert_channel.id}. Falling back to original channel.")

    await interaction.edit_original_response(content=None, embed=embed, view=view)

@bot.tree.command(name="deal", description="Search for game deals on IsThereAnyDeal")
@app_commands.describe(
    game_name="The name of the game you are looking for",
    country="The country code to search deals for (e.g. US, GB, DE)",
    platform="Filter results for PC or specific Console platforms"
)
@app_commands.choices(country=[
    app_commands.Choice(name="United States", value="US"),
    app_commands.Choice(name="United Kingdom", value="GB"),
    app_commands.Choice(name="Canada", value="CA"),
    app_commands.Choice(name="Australia", value="AU"),
    app_commands.Choice(name="Germany", value="DE"),
    app_commands.Choice(name="France", value="FR")
], platform=[
    app_commands.Choice(name="PC (Storefronts like Steam, GOG, Epic)", value="pc"),
    app_commands.Choice(name="PlayStation 4", value="ps4"),
    app_commands.Choice(name="PlayStation 5", value="ps5"),
    app_commands.Choice(name="Xbox One", value="xboxone"),
    app_commands.Choice(name="Xbox Series X|S", value="xboxseries"),
    app_commands.Choice(name="Nintendo Switch", value="switch")
])
async def deal_command(interaction: discord.Interaction, game_name: str, country: str = "US", platform: str = "pc", wishlist_buttons: bool = True):
    try:
        await interaction.response.defer(ephemeral=True)

        # Branching search logic
        if platform == 'pc':
            search_results = await fetch_itad_data(bot.session, "games/search/v1", {"title": game_name})
        else:
            # NEXARDA Search
            resp = await fetch_nexarda_data(bot.session, "search", {"type": "products", "q": game_name})
            search_results = []
            if resp and resp.get('results'):
                # Filter results to include the requested platform
                for res in resp['results']:
                    if any(p['slug'] == platform for p in res.get('platforms', [])):
                        search_results.append({'id': res['id'], 'title': res['title'], 'slug': res['slug']})

        if not search_results:
            return await interaction.followup.send(f"No games found matching '{game_name}'.")

        # 2. Handle multiple results
        if len(search_results) == 1:
            game = search_results[0]
            await show_deals(interaction, game['id'], game['title'], country, slug=game.get('slug'), platform=platform)
        else:
            view = GameSelectView(search_results, interaction.user, country, platform=platform)
            await interaction.followup.send(
                f"Multiple results found for '{game_name}'. Please select one:", 
                view=view
            )
    except Exception as e:
        logger.error(f"Error in itadbot command: {e}")
        # Provide specific error message for known parameter errors
        error_msg = str(e) if isinstance(e, ValueError) else "An error occurred while searching for the game."
        return await interaction.followup.send(error_msg)

# --- Wishlist Commands ---
wishlist_group = app_commands.Group(name="wishlist", description="Manage your game wishlist")

@wishlist_group.command(name="add", description="Add a game to your personal wishlist for sale alerts")
@app_commands.describe(
    game_name="The name of the game", 
    country="The region to track prices for",
    alert_method="How should the bot notify you?",
    platform="Choose the platform for this game"
)
@app_commands.choices(country=[
    app_commands.Choice(name="United States", value="US"),
    app_commands.Choice(name="United Kingdom", value="GB"),
    app_commands.Choice(name="Canada", value="CA"),
    app_commands.Choice(name="Australia", value="AU"),
    app_commands.Choice(name="Germany", value="DE"),
    app_commands.Choice(name="France", value="FR")
], alert_method=[
    app_commands.Choice(name="Direct Message", value="dm"),
    app_commands.Choice(name="Mention in this channel", value="mention")
], platform=[
    app_commands.Choice(name="PC", value="pc"),
    app_commands.Choice(name="PlayStation 4", value="ps4"),
    app_commands.Choice(name="PlayStation 5", value="ps5"),
    app_commands.Choice(name="Xbox One", value="xboxone"),
    app_commands.Choice(name="Xbox Series X|S", value="xboxseries"),
    app_commands.Choice(name="Nintendo Switch", value="switch")
])
async def wishlist_add(interaction: discord.Interaction, game_name: str, country: str = "US", alert_method: str = "dm", platform: str = "pc"):
    await interaction.response.defer()
    
    if platform == 'pc':
        search_results = await fetch_itad_data(bot.session, "games/search/v1", {"title": game_name})
    else:
        resp = await fetch_nexarda_data(bot.session, "search", {"type": "products", "q": game_name})
        search_results = []
        if resp and resp.get('results'):
            for res in resp['results']:
                if any(p['slug'] == platform for p in res.get('platforms', [])):
                    search_results.append({'id': res['id'], 'title': res['title'], 'slug': res['slug']})

    if not search_results:
        return await interaction.followup.send(f"No games found matching '{game_name}'.")

    if len(search_results) == 1:
        game = search_results[0]
        guild_id = interaction.guild_id if alert_method == "mention" else None
        channel_id = None
        
        if alert_method == "mention":
            # Check if a default server channel is configured
            async with bot.db.execute("SELECT alert_channel_id FROM guild_settings WHERE guild_id = ?", (interaction.guild_id,)) as cursor:
                row = await cursor.fetchone()
                channel_id = row[0] if row else interaction.channel_id

        await bot.db.execute(
            "INSERT OR REPLACE INTO wishlist (user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0)",
            (interaction.user.id, game['id'], game['title'], country, alert_method, guild_id, channel_id)
        )
        await bot.db.commit()
        msg = f"✅ Added **{game['title']}** ({country}) to your wishlist!"
        msg += " I'll DM you when it goes on sale." if alert_method == "dm" else f" I'll mention you in <#{channel_id}> when it goes on sale."
        await interaction.followup.send(msg)
    else:
        view = GameSelectView(search_results, interaction.user, country, action="add", alert_method=alert_method)
        await interaction.followup.send(f"Multiple results for '{game_name}'. Select which to add:", view=view)

@wishlist_group.command(name="update_alert", description="Change notification preference for a game or your whole wishlist")
@app_commands.describe(
    alert_method="How should the bot notify you?",
    game_id="The game ID or title to update (leave empty to update all)"
)
@app_commands.choices(alert_method=[
    app_commands.Choice(name="Direct Message", value="dm"),
    app_commands.Choice(name="Mention in this channel", value="mention")
])
async def wishlist_update_alert(interaction: discord.Interaction, alert_method: str, game_id: str = None):
    guild_id = interaction.guild_id if alert_method == "mention" else None
    channel_id = interaction.channel_id if alert_method == "mention" else None
    
    if alert_method == "mention" and not guild_id:
        return await interaction.response.send_message("❌ You can only set 'mention' alerts within a server channel.", ephemeral=True)

    if game_id:
        cursor = await bot.db.execute(
            "UPDATE wishlist SET alert_method = ?, guild_id = ?, channel_id = ? WHERE user_id = ? AND (game_id = ? OR game_title = ?)",
            (alert_method, guild_id, channel_id, interaction.user.id, game_id, game_id)
        )
        if cursor.rowcount == 0:
            return await interaction.response.send_message(f"Could not find '{game_id}' in your wishlist.", ephemeral=True)
        msg = f"✅ Updated alert method to **{alert_method}** for '{game_id}'."
    else:
        await bot.db.execute(
            "UPDATE wishlist SET alert_method = ?, guild_id = ?, channel_id = ? WHERE user_id = ?",
            (alert_method, guild_id, channel_id, interaction.user.id)
        )
        msg = f"✅ Updated alert method to **{alert_method}** for all games in your wishlist."
    
    await bot.db.commit()
    await interaction.response.send_message(msg, ephemeral=True)

@wishlist_group.command(name="list", description="View your current wishlist")
async def wishlist_list(interaction: discord.Interaction):
    async with bot.db.execute("SELECT game_title, country, alert_method FROM wishlist WHERE user_id = ?", (interaction.user.id,)) as cursor:
        items = await cursor.fetchall()
    
    if not items:
        return await interaction.response.send_message("Your wishlist is empty!", ephemeral=True)
    
    list_str = "\n".join([f"• {item[0]} ({item[1]}) - Alert: **{item[2]}**" for item in items])
    await interaction.response.send_message(f"### Your Wishlist:\n{list_str}", ephemeral=True)

@wishlist_group.command(name="remove", description="Remove a game from your wishlist")
async def wishlist_remove(interaction: discord.Interaction, game_id: str):
    # Use LOWER() for case-insensitive matching and check rowcount to confirm deletion
    cursor = await bot.db.execute(
        "DELETE FROM wishlist WHERE user_id = ? AND (LOWER(game_id) = LOWER(?) OR LOWER(game_title) = LOWER(?))",
        (interaction.user.id, game_id, game_id)
    )
    await bot.db.commit()
    
    if cursor.rowcount > 0:
        await interaction.response.send_message(f"✅ Removed '{game_id}' from your wishlist.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"❌ Could not find '{game_id}' in your wishlist. Please use the exact title (e.g., 'Inscryption') without the country suffix.",
            ephemeral=True
        )

@wishlist_group.command(name="clear", description="Remove ALL games from your wishlist")
async def wishlist_clear(interaction: discord.Interaction):
    await bot.db.execute("DELETE FROM wishlist WHERE user_id = ?", (interaction.user.id,))
    await bot.db.commit()
    await interaction.response.send_message("✅ Your entire wishlist has been cleared.", ephemeral=True)

@bot.tree.command(name="force_check", description="[Admin Only] Manually trigger a wishlist price check")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def force_check(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ You do not have permission to run this command.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    logger.info(f"Manual check triggered by {interaction.user}")
    
    await bot.check_wishlists()
    
    await interaction.followup.send("✅ Manual wishlist check completed. Check logs for details.", ephemeral=True)

@bot.tree.command(name="set_alert_channel", description="[Admin Only] Set the channel where search results are sent")
@app_commands.describe(channel="The channel for deal results")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def set_alert_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild_id:
        return await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ You need 'Manage Server' permissions to do this!", ephemeral=True)
        
    await bot.db.execute(
        "INSERT OR REPLACE INTO guild_settings (guild_id, alert_channel_id) VALUES (?, ?)",
        (interaction.guild_id, channel.id)
    )
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Search results will now be sent to {channel.mention}.", ephemeral=True)

if __name__ == "__main__":
    logger.info("=== Bot Startup Initiated ===")
    
    if not DISCORD_TOKEN or not ITAD_API_KEY:
        logger.error("DISCORD_TOKEN or ITAD_API_KEY not found in environment variables.")
        import sys
        sys.exit(1)
    else:
        logger.info("Environment verified. Connecting to Discord...")
        try:
            bot.run(DISCORD_TOKEN)
        except Exception as e:
            logger.critical(f"Failed to start bot: {e}")