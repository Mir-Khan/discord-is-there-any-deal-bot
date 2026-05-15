# TODO: Wishlist feature, Alerting, Notice for which platform a key is sold for (e.g. Steam key or GOG key)
import logging
import sys

# Configure logging immediately before any other imports
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s', stream=sys.stdout)
logger = logging.getLogger('itad_bot')
logger.info("Python process started. Initializing imports...")

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

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
        super().__init__(command_prefix="!", intents=intents)
        self.session = None

    async def setup_hook(self):
        # Initialize the aiohttp session here for persistence
        self.session = aiohttp.ClientSession()
        # Syncs slash commands with Discord.
        logger.info("Syncing slash commands...")
        await self.tree.sync()
        logger.info(f"Slash commands synced for {self.user}")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("------")

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()

bot = ITADBot()

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

class GameSelectView(discord.ui.View):
    """A view containing a dropdown for selecting a game from search results."""
    def __init__(self, games, author, country):
        super().__init__(timeout=60)
        self.author = author
        self.country = country
        options = [
            discord.SelectOption(label=g['title'], value=g['id'], description=f"ID: {g['id']}")
            for g in games[:25] # Discord limits select menus to 25 options
        ]
        self.select = discord.ui.Select(placeholder="Choose the correct game...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user != self.author:
            return await interaction.response.send_message("This isn't your search!", ephemeral=True)
        
        game_id = self.select.values[0]
        game_title = next(o.label for o in self.select.options if o.value == game_id)
        await interaction.response.defer()
        await show_deals(interaction, game_id, game_title, self.country)

async def show_deals(interaction, game_id, game_title, country):
    """Fetches deals for a specific game ID and edits the original response with an embed."""
    logger.info(f"Fetching deals for '{game_title}' (ID: {game_id}) in region: {country}")
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
        url=f"https://isthereanydeal.com/game/{game_id}/info/",
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
        # Use the currency returned by the API instead of hardcoded $
        currency_code = deal['price'].get('currency', 'USD')
        currency_symbol = CURRENCY_SYMBOLS.get(currency_code, currency_code)
        current_price = f"{currency_symbol}{deal['price']['amount']:.2f}"
        regular_price = f"{currency_symbol}{deal['regular']['amount']:.2f}"
        cut = f"{deal['cut']}% off"
        link = deal['url']
        
        embed.add_field(
            name=shop_name,
            value=f"**{current_price}** (~~{regular_price}~~)\n{cut} · [Link]({link})",
            inline=True
        )

    await interaction.edit_original_response(content=None, embed=embed, view=None)

@bot.tree.command(name="itadbot", description="Search for game deals on IsThereAnyDeal")
@app_commands.describe(
    game_name="The name of the game you are looking for",
    country="The country code to search deals for (e.g. US, GB, DE)"
)
@app_commands.choices(country=[
    app_commands.Choice(name="United States", value="US"),
    app_commands.Choice(name="United Kingdom", value="GB"),
    app_commands.Choice(name="Canada", value="CA"),
    app_commands.Choice(name="Australia", value="AU"),
    app_commands.Choice(name="Germany", value="DE"),
    app_commands.Choice(name="France", value="FR")
])
async def itadbot(interaction: discord.Interaction, game_name: str, country: str = "US"):
    try:
        await interaction.response.defer()

        # 1. Search for the game
        search_results = await fetch_itad_data(bot.session, "games/search/v1", {"title": game_name})

        if not search_results:
            return await interaction.followup.send(f"No games found matching '{game_name}'.")

        # 2. Handle multiple results
        if len(search_results) == 1:
            game = search_results[0]
            await show_deals(interaction, game['id'], game['title'], country)
        else:
            view = GameSelectView(search_results, interaction.user, country)
            await interaction.followup.send(
                f"Multiple results found for '{game_name}'. Please select one:", 
                view=view
            )
    except Exception as e:
        logger.error(f"Error in itadbot command: {e}")
        # Provide specific error message for known parameter errors
        error_msg = str(e) if isinstance(e, ValueError) else "An error occurred while searching for the game."
        return await interaction.followup.send(error_msg)

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