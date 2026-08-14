import logging
import sys

# Configure logging immediately before any other imports
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s', stream=sys.stdout)
logger = logging.getLogger('itad_bot')
logger.info("Python process started. Initializing imports...")

import asyncio
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

# ITAD country codes map to currencies for NEXARDA, which prices by currency rather than country.
COUNTRY_CURRENCY = {
    "US": "USD",
    "GB": "GBP",
    "CA": "CAD",
    "AU": "AUD",
    "DE": "EUR",
    "FR": "EUR",
}

def country_to_currency(country):
    return COUNTRY_CURRENCY.get(country, "USD")

# NEXARDA reports console offers under generic family names rather than per-generation
# (e.g. no distinction between PS4/PS5 or Xbox One/Series X|S), so we track at the family level.
NEXARDA_PLATFORMS = ["playstation", "xbox", "nintendo"]
# Cap concurrent NEXARDA requests during the wishlist scan so a large wishlist doesn't hammer the API.
NEXARDA_CHECK_CONCURRENCY = 8
NEXARDA_OFFER_PLATFORM_TAGS = {
    "playstation": {"PlayStation"},
    "xbox": {"Xbox", "Xbox Play Anywhere"},
    "nintendo": {"Nintendo"},
}
NEXARDA_SEARCH_PLATFORM_SLUGS = {
    "playstation": {"playstation"},
    "xbox": {"xbox", "xbox-play-anywhere"},
    "nintendo": {"nintendo"},
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
                min_discount_percent INTEGER DEFAULT 0,
                max_price REAL DEFAULT NULL,
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
            if "min_discount_percent" not in columns:
                await self.db.execute("ALTER TABLE wishlist ADD COLUMN min_discount_percent INTEGER DEFAULT 0")
            if "max_price" not in columns:
                await self.db.execute("ALTER TABLE wishlist ADD COLUMN max_price REAL DEFAULT NULL")

        # Migration: collapse old generation-specific console platform values into the
        # family-level values NEXARDA's price data actually supports (ps4/ps5 -> playstation, etc).
        await self.db.execute("UPDATE wishlist SET platform = 'playstation' WHERE platform IN ('ps4', 'ps5')")
        await self.db.execute("UPDATE wishlist SET platform = 'xbox' WHERE platform IN ('xboxone', 'xboxseries')")
        await self.db.execute("UPDATE wishlist SET platform = 'nintendo' WHERE platform = 'switch'")

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

        # tasks.loop permanently stops rescheduling itself if the wrapped coroutine ever raises,
        # so any unexpected error (bad API response, network blip) must not escape this method.
        try:
            await self._run_wishlist_check()
        except Exception:
            logger.exception("Unhandled error during wishlist check; will retry on next scheduled run.")

    async def _run_wishlist_check(self):
        logger.info("Running periodic wishlist price check...")

        # 1. Fetch all wishlist items once and group them by platform, country, and game_id
        all_wishlist_items = {}
        async with self.db.execute(
            "SELECT user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform, min_discount_percent, max_price FROM wishlist"
        ) as cursor:
            for item in await cursor.fetchall():
                platform, country, game_id = item[10], item[3], item[1]
                all_wishlist_items.setdefault(platform, {}).setdefault(country, {}).setdefault(game_id, []).append(item)

        if not all_wishlist_items:
            logger.info("No items in wishlist. Skipping deal check.")
            return

        now = int(time.time())

        # 1. Process PC Deals (ITAD) - one batched request per country
        pc_items = all_wishlist_items.get('pc', {})
        for country, games_in_country in pc_items.items():
            game_ids = list(games_in_country.keys())
            try:
                response = await fetch_itad_data(
                    self.session, "games/prices/v2",
                    params={"country": country, "nondeals": 0, "vouchers": 1},
                    method='POST', json_data=game_ids
                )
            except Exception:
                logger.exception(f"Failed to fetch ITAD prices for country {country}; skipping this batch.")
                continue
            if response:
                for game_result in response:
                    try:
                        await self.process_itad_alert(game_result, games_in_country, now)
                    except Exception:
                        logger.exception(f"Failed to process ITAD alert for game {game_result.get('id')}; skipping.")

        # 2. Process Console Deals (NEXARDA) - one request per game, run concurrently with a capped pool
        # since there's no batch endpoint for prices.
        console_jobs = [
            (plat, country, game_id, watchers)
            for plat in NEXARDA_PLATFORMS
            for country, games_in_country in all_wishlist_items.get(plat, {}).items()
            for game_id, watchers in games_in_country.items()
        ]

        if console_jobs:
            semaphore = asyncio.Semaphore(NEXARDA_CHECK_CONCURRENCY)

            async def check_one(plat, country, game_id, watchers):
                async with semaphore:
                    try:
                        offers = await fetch_nexarda_offers(self.session, game_id, plat, currency=country_to_currency(country))
                    except Exception:
                        logger.exception(f"Failed to fetch NEXARDA prices for {plat}/{game_id}; skipping.")
                        return
                    if offers:
                        try:
                            await self.process_nexarda_alert(offers, watchers, now)
                        except Exception:
                            logger.exception(f"Failed to process NEXARDA alert for {plat}/{game_id}; skipping.")

            await asyncio.gather(*(check_one(*job) for job in console_jobs))

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

    async def process_nexarda_alert(self, offers, watchers, now):
        if not offers or not watchers: return

        # Find the best current price among on-sale offers
        on_sale = [o for o in offers if o['discount'] > 0]
        if not on_sale:
            return
        best_deal = min(on_sale, key=lambda x: x['price'])
        game_id = watchers[0][1]

        await self.dispatch_alert(game_id, watchers=watchers,
                                  deal_url=best_deal['url'], shop_name=best_deal['store']['name'],
                                  price=best_deal['price'], currency=best_deal['currency'],
                                  cut=best_deal['discount'], expiry=None, now=now)

    async def dispatch_alert(self, game_id, watchers, deal_url, shop_name, price, currency, cut, expiry, now):
        title = watchers[0][2]
        platform = watchers[0][10]
        # Bucket recipients by (method, is_expiring) so each group gets an accurate message
        # instead of all recipients sharing whichever state the last watcher processed happened to be in.
        dm_alerts = {False: [], True: []}
        mention_alerts = {False: {}, True: {}}

        if expiry is not None:
            try: expiry = int(expiry)
            except: expiry = None

        for watcher in watchers:
            user_id, _, _, _, method, g_id, c_id, last_url, state, snoozed, _platform, min_discount, max_price = watcher

            if min_discount and cut < min_discount:
                continue
            if max_price is not None and price > max_price:
                continue

            is_new_deal = (deal_url != last_url)
            is_expiring = bool(expiry and (expiry - now) < 28800 and state == 1)

            if not is_new_deal and (snoozed or state == 2 or (state == 1 and not is_expiring)):
                continue

            if method == 'mention' and g_id and c_id:
                mention_alerts[is_expiring].setdefault((g_id, c_id), []).append(user_id)
            else:
                dm_alerts[is_expiring].append(user_id)

            # Update DB state
            new_alert_state = 1 if is_new_deal else (2 if is_expiring else state)
            await self.db.execute(
                "UPDATE wishlist SET last_deal_url = ?, alert_state = ?, is_snoozed = 0 WHERE user_id = ? AND game_id = ?",
                (deal_url, new_alert_state, user_id, game_id)
            )
        await self.db.commit()

        price_fmt = f"{CURRENCY_SYMBOLS.get(currency, '$')}{price:.2f}"
        view = WishlistActionView(game_id, title)

        def build_msg(is_expiring):
            prefix = "⏳ **Final Call! Deal Expiring Soon:**" if is_expiring else "🔔 **New Deal Alert!**"
            return f"{prefix}\n'{title}' ({platform.upper()}) is currently **{price_fmt}** ({cut}% off) at {shop_name}.\nLink: {deal_url}"

        for is_expiring, u_ids in dm_alerts.items():
            if not u_ids:
                continue
            msg = build_msg(is_expiring)
            for u_id in u_ids:
                try:
                    user = await self.fetch_user(u_id)
                    if user: await user.send(msg, view=view)
                except: pass

        for is_expiring, channels in mention_alerts.items():
            if not channels:
                continue
            msg = build_msg(is_expiring)
            for (gid, cid), u_ids in channels.items():
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

async def search_nexarda_games(session, query, platform):
    """Search NEXARDA for games available on the given console family (playstation/xbox/nintendo)."""
    resp = await fetch_nexarda_data(session, "search", {"type": "games", "q": query})
    if not resp or not resp.get('success'):
        return []

    wanted_slugs = NEXARDA_SEARCH_PLATFORM_SLUGS.get(platform, set())
    results = []
    for item in resp.get('results', {}).get('items', []):
        game_info = item.get('game_info') or {}
        plat_slugs = {p.get('slug') for p in game_info.get('platforms', [])}
        if plat_slugs & wanted_slugs:
            results.append({
                # Discord always returns select-menu values as strings, and this id is compared
                # against that later, so it must be a string here too (NEXARDA returns it as an int).
                'id': str(game_info.get('id')),
                'title': game_info.get('name') or item.get('title'),
                'slug': item.get('slug'),
            })
    return results

async def fetch_nexarda_offers(session, game_id, platform, currency='USD'):
    """Fetch currently available, purchasable offers for a NEXARDA game id on the given console family."""
    data = await fetch_nexarda_data(session, "prices", {"type": "game", "id": game_id, "currency": currency})
    if not data or not data.get('success'):
        return []

    wanted_tags = NEXARDA_OFFER_PLATFORM_TAGS.get(platform, set())
    offers = data.get('prices', {}).get('list', [])
    resolved_currency = data.get('prices', {}).get('currency', currency)
    return [
        {**o, 'currency': resolved_currency}
        for o in offers
        if o.get('available') and o.get('price', -1) >= 0 and o.get('platform') in wanted_tags
    ]

class GameSelectView(discord.ui.View):
    """A view containing a dropdown for selecting a game from search results."""
    def __init__(self, games, author, country, action="show", alert_method="dm", platform="pc", min_discount=0, max_price=None):
        super().__init__(timeout=60)
        self.author = author
        self.country = country
        self.action = action # "show" (deals) or "add" (wishlist)
        self.alert_method = alert_method
        self.platform = platform
        self.min_discount = min_discount
        self.max_price = max_price
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
                "INSERT OR REPLACE INTO wishlist (user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform, min_discount_percent, max_price) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?, ?, ?)",
                (interaction.user.id, game_id, game_title, self.country, self.alert_method, guild_id, channel_id, self.platform, self.min_discount or 0, self.max_price)
            )
            await bot.db.commit()
            msg = f"✅ Added **{game_title}** ({self.country}) to your wishlist!"
            msg += " I'll DM you when it goes on sale." if self.alert_method == "dm" else f" I'll mention you in <#{channel_id}> when it goes on sale."
            if self.min_discount:
                msg += f" Only when discount is ≥{self.min_discount}%."
            if self.max_price is not None:
                msg += f" Only when price is ≤{self.max_price:.2f}."

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
        offers = await fetch_nexarda_offers(bot.session, game_id, platform, currency=country_to_currency(country))

        if not offers:
            return await interaction.followup.send(f"No current deals found for {game_title} on this platform.")

        offers.sort(key=lambda x: x['price'])
        currency_symbol = CURRENCY_SYMBOLS.get(offers[0]['currency'], offers[0]['currency'])

        product = await fetch_nexarda_data(bot.session, "product", {"type": "game", "id": game_id})
        banner = None
        if product and product.get('success'):
            images = product.get('product', {}).get('images', {})
            banner = images.get('banner') or images.get('cover')

        embed = discord.Embed(
            title=f"{game_title} ({platform.capitalize()})",
            url=f"https://www.nexarda.com{slug}" if slug else f"https://www.nexarda.com/games/{game_id}",
            description=f"📉 **Lowest Price:** {currency_symbol}{offers[0]['price']:.2f}",
            color=0x3498db,
            timestamp=interaction.created_at
        )
        embed.set_author(name=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="Data provided by NEXARDA")
        if banner:
            embed.set_image(url=banner)

        for offer in offers[:10]:
            cut_str = f" ({offer['discount']}% off)" if offer['discount'] else ""
            embed.add_field(
                name=offer['store']['name'],
                value=f"**{currency_symbol}{offer['price']:.2f}**{cut_str}\n[Link]({offer['url']})",
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
    app_commands.Choice(name="PlayStation", value="playstation"),
    app_commands.Choice(name="Xbox", value="xbox"),
    app_commands.Choice(name="Nintendo Switch", value="nintendo")
])
async def deal_command(interaction: discord.Interaction, game_name: str, country: str = "US", platform: str = "pc", wishlist_buttons: bool = True):
    try:
        await interaction.response.defer(ephemeral=True)

        # Branching search logic
        if platform == 'pc':
            search_results = await fetch_itad_data(bot.session, "games/search/v1", {"title": game_name})
        else:
            search_results = await search_nexarda_games(bot.session, game_name, platform)

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
    platform="Choose the platform for this game",
    max_price="Only alert when the price drops to or below this amount",
    min_discount="Only alert when the discount is at least this percent"
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
    app_commands.Choice(name="PlayStation", value="playstation"),
    app_commands.Choice(name="Xbox", value="xbox"),
    app_commands.Choice(name="Nintendo Switch", value="nintendo")
])
async def wishlist_add(interaction: discord.Interaction, game_name: str, country: str = "US", alert_method: str = "dm", platform: str = "pc",
                        max_price: float = None, min_discount: app_commands.Range[int, 1, 99] = None):
    await interaction.response.defer()

    if platform == 'pc':
        search_results = await fetch_itad_data(bot.session, "games/search/v1", {"title": game_name})
    else:
        search_results = await search_nexarda_games(bot.session, game_name, platform)

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
            "INSERT OR REPLACE INTO wishlist (user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed, platform, min_discount_percent, max_price) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?, ?, ?)",
            (interaction.user.id, game['id'], game['title'], country, alert_method, guild_id, channel_id, platform, min_discount or 0, max_price)
        )
        await bot.db.commit()
        msg = f"✅ Added **{game['title']}** ({country}) to your wishlist!"
        msg += " I'll DM you when it goes on sale." if alert_method == "dm" else f" I'll mention you in <#{channel_id}> when it goes on sale."
        if min_discount:
            msg += f" Only when discount is ≥{min_discount}%."
        if max_price is not None:
            msg += f" Only when price is ≤{max_price:.2f}."
        await interaction.followup.send(msg)
    else:
        view = GameSelectView(search_results, interaction.user, country, action="add", alert_method=alert_method,
                               platform=platform, min_discount=min_discount or 0, max_price=max_price)
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

@wishlist_group.command(name="threshold", description="Set a minimum discount % or maximum price for alerts on a wishlist item")
@app_commands.describe(
    game_id="The game ID or title to update (leave empty to update all)",
    max_price="Only alert when the price drops to or below this amount (omit to clear)",
    min_discount="Only alert when the discount is at least this percent (omit to clear)"
)
async def wishlist_threshold(interaction: discord.Interaction, game_id: str = None, max_price: float = None, min_discount: app_commands.Range[int, 1, 99] = None):
    if game_id:
        cursor = await bot.db.execute(
            "UPDATE wishlist SET min_discount_percent = ?, max_price = ? WHERE user_id = ? AND (game_id = ? OR game_title = ?)",
            (min_discount or 0, max_price, interaction.user.id, game_id, game_id)
        )
        if cursor.rowcount == 0:
            return await interaction.response.send_message(f"Could not find '{game_id}' in your wishlist.", ephemeral=True)
        msg = f"✅ Updated alert thresholds for '{game_id}'."
    else:
        await bot.db.execute(
            "UPDATE wishlist SET min_discount_percent = ?, max_price = ? WHERE user_id = ?",
            (min_discount or 0, max_price, interaction.user.id)
        )
        msg = "✅ Updated alert thresholds for all games in your wishlist."

    if min_discount:
        msg += f" Only alerting when discount is ≥{min_discount}%."
    if max_price is not None:
        msg += f" Only alerting when price is ≤{max_price:.2f}."
    if not min_discount and max_price is None:
        msg += " Thresholds cleared."

    await bot.db.commit()
    await interaction.response.send_message(msg, ephemeral=True)

@wishlist_group.command(name="list", description="View your current wishlist")
async def wishlist_list(interaction: discord.Interaction):
    async with bot.db.execute(
        "SELECT game_title, country, alert_method, min_discount_percent, max_price FROM wishlist WHERE user_id = ?",
        (interaction.user.id,)
    ) as cursor:
        items = await cursor.fetchall()

    if not items:
        return await interaction.response.send_message("Your wishlist is empty!", ephemeral=True)

    def fmt(item):
        title, country, alert_method, min_discount, max_price = item
        extras = []
        if min_discount:
            extras.append(f"≥{min_discount}% off")
        if max_price is not None:
            extras.append(f"≤{max_price:.2f}")
        extra_str = f" [{', '.join(extras)}]" if extras else ""
        return f"• {title} ({country}) - Alert: **{alert_method}**{extra_str}"

    list_str = "\n".join(fmt(item) for item in items)
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