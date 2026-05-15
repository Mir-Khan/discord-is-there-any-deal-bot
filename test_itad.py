import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from main import fetch_itad_data, deal_command, GameSelectView, bot, wishlist_add, wishlist_list, wishlist_update_alert, WelcomeView, DealView, wishlist_remove, wishlist_clear, force_check, set_alert_channel
import discord
import os
import datetime
from dotenv import load_dotenv

@pytest.mark.asyncio
async def test_fetch_itad_data_success():
    # Mocking aiohttp.ClientSession and the response
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json.return_value = [{"title": "Test Game", "id": "123"}]
    
    # Setup the context manager mock
    mock_session.request.return_value.__aenter__.return_value = mock_response

    result = await fetch_itad_data(mock_session, "games/search/v1", {"title": "Test"})
    
    assert result is not None
    assert result[0]["title"] == "Test Game"
    mock_session.request.assert_called_once()

@pytest.mark.asyncio
async def test_fetch_itad_data_unsupported_country():
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_response = AsyncMock()
    mock_response.status = 400
    mock_response.text.return_value = '{"error": "Invalid country code"}'
    
    mock_session.request.return_value.__aenter__.return_value = mock_response

    with pytest.raises(ValueError, match="is not supported"):
        await fetch_itad_data(mock_session, "games/prices/v2", {"country": "XX"})

# --- Mock Data for ITAD API Responses ---

MOCK_INSCRYPTION_SEARCH_RESULT = [
    {"title": "Inscryption", "id": "inscryption"}
]

MOCK_INSCRYPTION_OVERVIEW_DATA = {
    "prices": [
        {
            "id": "inscryption",
            "lowest": {
                "shop": {"name": "Steam"},
                "price": {"amount": 5.99, "currency": "USD"}
            },
            "bundled": 1,
            "image": {
                "thumb": "https://img.isthereanydeal.com/img/thumb/12345.jpg"
            }
        }
    ],
    "bundles": [
        {"title": "Humble Choice May 2024", "url": "https://isthereanydeal.com/specials/#/filter:id/123"}
    ]
}

MOCK_INSCRYPTION_DEALS_DATA = [
    {
        "id": "inscryption",
        "deals": [
            {
                "shop": {"name": "Steam"},
                "price": {"amount": 19.99, "currency": "USD"},
                "regular": {"amount": 19.99, "currency": "USD"},
                "cut": 0,
                "url": "https://store.steampowered.com/app/1062520/Inscryption/"
            },
            {
                "shop": {"name": "GOG"},
                "price": {"amount": 14.99, "currency": "USD"},
                "regular": {"amount": 19.99, "currency": "USD"},
                "cut": 25,
                "url": "https://www.gog.com/game/inscryption"
            }
        ]
    }
]

MOCK_AC_ORIGINS_SEARCH_RESULT = [
    {"title": "Assassin's Creed Origins", "id": "assassinscreedorigins"}
]

MOCK_AC_ORIGINS_OVERVIEW_DATA = {
    "prices": [
        {
            "id": "assassinscreedorigins",
            "lowest": {
                "shop": {"name": "Ubisoft Store"},
                "price": {"amount": 11.99, "currency": "USD"}
            },
            "bundled": 0,
            "image": {
                "thumb": "https://img.isthereanydeal.com/img/thumb/67890.jpg"
            }
        }
    ],
    "bundles": []
}

MOCK_AC_ORIGINS_DEALS_DATA = [
    {
        "id": "assassinscreedorigins",
        "deals": [
            {
                "shop": {"name": "Ubisoft Store"},
                "price": {"amount": 11.99, "currency": "USD"},
                "regular": {"amount": 59.99, "currency": "USD"},
                "cut": 80,
                "url": "https://store.ubi.com/us/assassins-creed-origins/"
            },
            {
                "shop": {"name": "Steam"},
                "price": {"amount": 17.99, "currency": "USD"},
                "regular": {"amount": 59.99, "currency": "USD"},
                "cut": 70,
                "url": "https://store.steampowered.com/app/582160/Assassins_Creed_Origins/"
            }
        ]
    }
]

MOCK_AC_SERIES_SEARCH_RESULTS = [
    {"title": "Assassin's Creed Origins", "id": "assassinscreedorigins"},
    {"title": "Assassin's Creed Odyssey", "id": "assassinscreedodyssey"},
    {"title": "Assassin's Creed Valhalla", "id": "assassinscreedvalhalla"}
]

class MockAiosqliteExecute:
    """A mock helper that handles both 'async with' and 'await' for aiosqlite.execute()."""
    def __init__(self, cursor_mock):
        self.cursor = cursor_mock
    def __await__(self):
        async def _yield_cursor(): return self.cursor
        return _yield_cursor().__await__()
    async def __aenter__(self):
        return self.cursor
    async def __aexit__(self, *args):
        pass

# --- Helper for Mocking Discord Interaction ---
class MockInteraction:
    def __init__(self, user_id=123):
        self.created_at = datetime.datetime.now(datetime.timezone.utc)
        self.response = AsyncMock()
        self.followup = AsyncMock()
        self.guild_id = 456
        self.channel_id = 789
        self.user = MagicMock()
        self.user.id = user_id
        # This is important for the GameSelectView's author check
        self.user.__eq__.side_effect = lambda other: getattr(other, 'id', None) == self.user.id
        
        self.followup.send.side_effect = self._capture_followup
        
        self.sent_view = None
        self.original_response_content = None
        self.original_response_embed = None
        self.original_response_view = None

    async def edit_message(self, *args, **kwargs):
        # Mock for interaction.response.edit_message
        await self.response.edit_message(*args, **kwargs)

    async def _capture_followup(self, *args, **kwargs):
        if 'view' in kwargs:
            self.sent_view = kwargs['view']

    async def edit_original_response(self, *args, **kwargs):
        self.original_response_content = kwargs.get('content')
        self.original_response_embed = kwargs.get('embed')
        self.original_response_view = kwargs.get('view')
        await self.response.edit_original_response(*args, **kwargs)

@pytest.fixture(autouse=True)
def mock_itad_api_key():
    # Ensure ITAD_API_KEY is set for tests, even if .env isn't fully loaded
    # This prevents errors in fetch_itad_data if ITAD_API_KEY is None
    os.environ['ITAD_API_KEY'] = 'mock_api_key'
    load_dotenv() # Reload dotenv to pick up the mock key if not already set

@pytest.mark.asyncio
async def test_itadbot_single_game_search_inscryption():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = None # Game not on wishlist
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)
    
    # Mock fetch_itad_data for search and then for deals
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        # Mock the bot's session
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'db', mock_db)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        # Search, Overview, then Prices
        mock_response.json.side_effect = [MOCK_INSCRYPTION_SEARCH_RESULT, MOCK_INSCRYPTION_OVERVIEW_DATA, MOCK_INSCRYPTION_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await deal_command.callback(mock_interaction, "Inscryption")

        # Assertions
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.response.edit_original_response.assert_called_once()
        
        embed = mock_interaction.original_response_embed
        assert embed is not None
        assert "Inscryption" in embed.title
        assert "12345.jpg" in embed.image.url
        assert "Historical Low" in embed.description
        assert "GOG" in embed.fields[2].name
        assert "$14.99" in embed.fields[2].value

        # Verify DealView is attached
        assert isinstance(mock_interaction.original_response_view, DealView)
        assert mock_interaction.original_response_view.is_on_wishlist is False

@pytest.mark.asyncio
async def test_itadbot_game_in_series_search_ac_origins():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = None
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'db', mock_db)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        # Search, Overview, then Prices
        mock_response.json.side_effect = [MOCK_AC_ORIGINS_SEARCH_RESULT, MOCK_AC_ORIGINS_OVERVIEW_DATA, MOCK_AC_ORIGINS_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await deal_command.callback(mock_interaction, "Assassin's Creed: Origins")

        # Assertions
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.response.edit_original_response.assert_called_once()
        
        embed = mock_interaction.original_response_embed
        assert embed is not None
        assert "Assassin's Creed Origins" in embed.title
        assert "67890.jpg" in embed.image.url
        assert "Historical Low" in embed.description
        assert "Ubisoft Store" in embed.fields[1].name
        assert "$11.99" in embed.fields[1].value

# --- Wishlist Feature Tests ---

@pytest.mark.asyncio
async def test_wishlist_add_single_result():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'db', mock_db)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = MOCK_INSCRYPTION_SEARCH_RESULT
        mock_session.request.return_value.__aenter__.return_value = mock_response

        # Setup mock_db.execute to return a compatible mock
        mock_cursor = AsyncMock()
        mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

        await wishlist_add.callback(mock_interaction, "Inscryption", country="US", alert_method="dm")

        # Verify DB insertion
        assert mock_db.execute.call_count >= 1
        args = mock_db.execute.call_args[0]
        assert "INSERT OR REPLACE INTO wishlist" in args[0]
        assert args[1] == (mock_interaction.user.id, "inscryption", "Inscryption", "US", "dm", None, None)
        
        mock_interaction.followup.send.assert_called_once_with(
            "✅ Added **Inscryption** (US) to your wishlist! I'll DM you when it goes on sale."
        )

@pytest.mark.asyncio
async def test_wishlist_list_empty():
    mock_interaction = MockInteraction()
    mock_db = AsyncMock()
    mock_cursor = AsyncMock()
    # Mock the async context manager for db.execute. Use MagicMock so the call itself isn't a coroutine.
    mock_db.execute = MagicMock()
    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_cursor
    mock_cursor.fetchall.return_value = []
    mock_db.execute.return_value = mock_context_manager

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await wishlist_list.callback(mock_interaction)
        
        mock_interaction.response.send_message.assert_called_once_with(
            "Your wishlist is empty!", ephemeral=True
        )

@pytest.mark.asyncio
async def test_wishlist_list_with_items():
    mock_interaction = MockInteraction()
    mock_db = AsyncMock()
    mock_cursor = AsyncMock()
    # Mock the async context manager for db.execute. Use MagicMock so the call itself isn't a coroutine.
    mock_db.execute = MagicMock()
    mock_context_manager = AsyncMock()
    mock_context_manager.__aenter__.return_value = mock_cursor
    mock_cursor.fetchall.return_value = [("Inscryption", "US", "dm"), ("Slay the Spire", "GB", "mention")]
    mock_db.execute.return_value = mock_context_manager

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await wishlist_list.callback(mock_interaction)
        
        message = mock_interaction.response.send_message.call_args[0][0]
        assert "Inscryption (US) - Alert: **dm**" in message
        assert "Slay the Spire (GB) - Alert: **mention**" in message

@pytest.mark.asyncio
async def test_wishlist_update_alert_all():
    mock_interaction = MockInteraction()
    mock_db = AsyncMock()
    
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await wishlist_update_alert.callback(mock_interaction, alert_method="mention", game_id=None)

        mock_db.execute.assert_called_once()
        args = mock_db.execute.call_args[0]
        assert "UPDATE wishlist SET alert_method = ?" in args[0]
        # Verify it uses the interaction's channel/guild for mentions
        assert args[1] == ("mention", mock_interaction.guild_id, mock_interaction.channel_id, mock_interaction.user.id)
        
        mock_interaction.response.send_message.assert_called_once_with(
            "✅ Updated alert method to **mention** for all games in your wishlist.", ephemeral=True
        )

@pytest.mark.asyncio
async def test_game_select_view_add_to_wishlist():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = [None]

    # Setup mock_db.execute to handle both await and async with
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        view = GameSelectView(MOCK_AC_SERIES_SEARCH_RESULTS, mock_interaction.user, "US", action="add", alert_method="mention")
        
        with patch.object(type(view.select), 'values', new_callable=PropertyMock) as mock_values:
            mock_values.return_value = ["assassinscreedorigins"]
            await view.select_callback(mock_interaction)

        # Check DB update for specific game selection
        # Called once for SELECT (channel settings) and once for INSERT
        assert mock_db.execute.call_count >= 1
        args = mock_db.execute.call_args[0]
        assert "INSERT OR REPLACE INTO wishlist" in args[0]
        assert "assassinscreedorigins" in args[1]
        mock_interaction.response.edit_message.assert_called_once()

@pytest.mark.asyncio
async def test_itadbot_series_search_presents_selection():
    mock_interaction = MockInteraction()
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)

        # Mock search to return multiple results
        mock_session.request.return_value.__aenter__.return_value.status = 200
        mock_session.request.return_value.__aenter__.return_value.json.return_value = MOCK_AC_SERIES_SEARCH_RESULTS

        await deal_command.callback(mock_interaction, "Assassin's Creed")

        # Assertions
        mock_interaction.response.defer.assert_called_once()
        mock_interaction.followup.send.assert_called_once()
        
        # Check that a GameSelectView was sent
        assert isinstance(mock_interaction.sent_view, GameSelectView)
        assert len(mock_interaction.sent_view.select.options) == len(MOCK_AC_SERIES_SEARCH_RESULTS)
        assert "Assassin's Creed Origins" in mock_interaction.sent_view.select.options[0].label

@pytest.mark.asyncio
async def test_itadbot_series_selection_leads_to_deals():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = None
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'db', mock_db)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        # 1. Search results, 2. Overview, 3. Prices
        mock_response.json.side_effect = [MOCK_AC_SERIES_SEARCH_RESULTS, MOCK_AC_ORIGINS_OVERVIEW_DATA, MOCK_AC_ORIGINS_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await deal_command.callback(mock_interaction, "Assassin's Creed")

        # Simulate user selecting "Assassin's Creed Origins"
        sent_view = mock_interaction.sent_view
        
        # Mock the 'values' property of the select menu
        with patch.object(type(sent_view.select), 'values', new_callable=PropertyMock) as mock_values:
            mock_values.return_value = ["assassinscreedorigins"]
            # Manually call the select_callback to simulate user interaction
            await sent_view.select_callback(mock_interaction)

        # Assertions for the final deal display
        mock_interaction.response.edit_original_response.assert_called_once()
        embed = mock_interaction.original_response_embed
        assert embed is not None
        assert "Assassin's Creed Origins" in embed.title
        assert "67890.jpg" in embed.image.url
        assert "Historical Low" in embed.description
        assert "Ubisoft Store" in embed.fields[1].name
        assert "$11.99" in embed.fields[1].value
        assert isinstance(mock_interaction.original_response_view, DealView)

@pytest.mark.asyncio
async def test_check_wishlists_sends_alerts():
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_user = AsyncMock(spec=discord.User)
    mock_user.send = AsyncMock()
    mock_channel = AsyncMock(spec=discord.TextChannel)
    mock_channel.send = AsyncMock()

    # 1. Mock DB SELECT for all wishlist items (batch fetch)
    mock_cursor = AsyncMock()
    # 10 columns: user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed
    mock_cursor.fetchall.return_value = [
        (123, "inscryption", "Inscryption", "US", "dm", None, None, None, 0, 0),
        (456, "inscryption", "Inscryption", "US", "mention", 777, 789, None, 0, 0)
    ]
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    # Mock fetch_itad_data to return deal data
    mock_response_itad = AsyncMock()
    mock_response_itad.status = 200
    mock_response_itad.json.return_value = MOCK_INSCRYPTION_DEALS_DATA
    mock_session.request.return_value.__aenter__.return_value = mock_response_itad

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'fetch_user', AsyncMock(return_value=mock_user))
        mp.setattr(bot, 'get_channel', MagicMock(return_value=mock_channel))
        mp.setattr(bot, 'fetch_channel', AsyncMock(return_value=mock_channel))
        mp.setattr(bot.check_wishlists, 'start', MagicMock()) # Prevent actual start
        mp.setattr(bot.check_wishlists, 'stop', MagicMock()) # Prevent actual stop

        await bot.check_wishlists()

        # Verify DM sent to user 123
        bot.fetch_user.assert_called_once_with(123)
        mock_user.send.assert_called_once()
        
        # Verify Mention sent (Grouped pings for channel 789)
        mock_channel.send.assert_called_once()
        sent_msg = mock_channel.send.call_args.kwargs['content']
        assert "<@456>" in sent_msg
        assert "Deal Alert!" in sent_msg

@pytest.mark.asyncio
async def test_check_wishlists_sends_final_call_alert():
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    mock_user = AsyncMock()
    
    # Mock deal expiring in 2 hours (7200 seconds)
    expiry_time = int(datetime.datetime.now().timestamp()) + 7200
    deal_url = "https://store.steampowered.com/app/1062520/Inscryption/"
    
    # 1. Mock DB SELECT for all wishlist items
    mock_cursor = AsyncMock()
    # user_id, game_id, game_title, country, alert_method, guild_id, channel_id, last_deal_url, alert_state, is_snoozed
    mock_cursor.fetchall.return_value = [
        (123, "inscryption", "Inscryption", "US", "dm", None, None, deal_url, 1, 0)
    ]
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    mock_response_itad = AsyncMock()
    mock_response_itad.status = 200
    # Inject expiry into the mock data
    expiring_data = [MOCK_INSCRYPTION_DEALS_DATA[0].copy()]
    expiring_data[0]['deals'][0]['expiry'] = expiry_time
    mock_response_itad.json.return_value = expiring_data
    mock_session.request.return_value.__aenter__.return_value = mock_response_itad

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'fetch_user', AsyncMock(return_value=mock_user))
        mp.setattr(bot.check_wishlists, 'start', MagicMock())
        mp.setattr(bot.check_wishlists, 'stop', MagicMock())

        await bot.check_wishlists()

        mock_user.send.assert_called_once()
        sent_msg = mock_user.send.call_args.args[0]
        assert "Final Call!" in sent_msg
        # Verify DB updated to state 2 (Expiry Sent)
        update_args = mock_db.execute.call_args_list[1][0]
        assert update_args[1][1] == 2 # new_state

@pytest.mark.asyncio
async def test_on_guild_join_sends_welcome():
    mock_guild = MagicMock(spec=discord.Guild)
    mock_channel = AsyncMock(spec=discord.TextChannel)
    mock_guild.system_channel = mock_channel
    mock_channel.permissions_for.return_value.send_messages = True

    await bot.on_guild_join(mock_guild)

    mock_channel.send.assert_called_once()
    embed = mock_channel.send.call_args.kwargs['embed']
    assert "Welcome to ITAD Deal Bot" in embed.title
    assert isinstance(mock_channel.send.call_args.kwargs['view'], WelcomeView)

@pytest.mark.asyncio
async def test_itadbot_without_wishlist_buttons():
    mock_interaction = MockInteraction()
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        # No DB mock needed as include_wishlist_buttons=False skips DB access
        
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.side_effect = [MOCK_INSCRYPTION_SEARCH_RESULT, MOCK_INSCRYPTION_OVERVIEW_DATA, MOCK_INSCRYPTION_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await deal_command.callback(mock_interaction, "Inscryption", wishlist_buttons=False)

        assert mock_interaction.original_response_view is None

@pytest.mark.asyncio
async def test_deal_view_add_dm():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        # Initially not on wishlist
        view = DealView("inscryption", "Inscryption", "US", is_on_wishlist=False, guild_id=None)
        
        # Trigger callback
        await view.add_dm_callback(mock_interaction)
        
        # Verify DB insertion
        assert mock_db.execute.call_count >= 1
        args = mock_db.execute.call_args[0]
        assert "INSERT OR REPLACE INTO wishlist" in args[0]
        assert "dm" in args[1]
        
        assert view.is_on_wishlist is True
        mock_interaction.response.edit_message.assert_called_once()
        mock_interaction.followup.send.assert_called_once()

@pytest.mark.asyncio
async def test_wishlist_remove_success():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    mock_cursor.rowcount = 1
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await wishlist_remove.callback(mock_interaction, "Inscryption")

        assert mock_db.execute.call_count >= 1
        args = mock_db.execute.call_args[0]
        assert "DELETE FROM wishlist" in args[0]
        assert "LOWER(game_id) = LOWER(?)" in args[0]
        mock_interaction.response.send_message.assert_called_once_with(
            "✅ Removed 'Inscryption' from your wishlist.", ephemeral=True
        )

@pytest.mark.asyncio
async def test_wishlist_remove_not_found():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    mock_cursor.rowcount = 0
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await wishlist_remove.callback(mock_interaction, "NonExistentGame")

        mock_interaction.response.send_message.assert_called_once()
        assert "Could not find" in mock_interaction.response.send_message.call_args[0][0]

@pytest.mark.asyncio
async def test_wishlist_clear():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await wishlist_clear.callback(mock_interaction)

        args = mock_db.execute.call_args[0]
        assert "DELETE FROM wishlist WHERE user_id = ?" in args[0]
        assert args[1][0] == mock_interaction.user.id
        mock_interaction.response.send_message.assert_called_once_with(
            "✅ Your entire wishlist has been cleared.", ephemeral=True
        )

@pytest.mark.asyncio
async def test_force_check_admin():
    mock_interaction = MockInteraction()
    mock_interaction.user.guild_permissions.administrator = True
    
    with patch.object(bot, 'check_wishlists', new_callable=AsyncMock) as mock_check:
        await force_check.callback(mock_interaction)
        mock_interaction.response.defer.assert_called_once()
        mock_check.assert_called_once()

@pytest.mark.asyncio
async def test_force_check_no_permission():
    mock_interaction = MockInteraction()
    mock_interaction.user.guild_permissions.administrator = False
    
    await force_check.callback(mock_interaction)
    mock_interaction.response.send_message.assert_called_once_with(
        "❌ You do not have permission to run this command.", ephemeral=True
    )

@pytest.mark.asyncio
async def test_deal_view_add_mention():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    # Mocking select for alert_channel_id
    mock_cursor.fetchone.return_value = [999] 
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        view = DealView("inscryption", "Inscryption", "US", is_on_wishlist=False, guild_id=456)
        
        await view.add_mention_callback(mock_interaction)
        
        # Verify DB insertion contains mention info
        # 1 for SELECT alert_channel_id, 1 for INSERT
        assert mock_db.execute.call_count >= 2
        
        # Check INSERT arguments
        insert_call = mock_db.execute.call_args_list[1]
        insert_args = insert_call[0]
        assert "mention" in insert_args[1]
        assert 999 in insert_args[1] # channel_id
        
        assert view.is_on_wishlist is True
        mock_interaction.response.edit_message.assert_called_once()

@pytest.mark.asyncio
async def test_deal_view_remove():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_cursor = AsyncMock()
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        # Initially on wishlist
        view = DealView("inscryption", "Inscryption", "US", is_on_wishlist=True)
        
        await view.remove_callback(mock_interaction)
        
        # Verify DB deletion
        assert mock_db.execute.call_count >= 1
        args = mock_db.execute.call_args[0]
        assert "DELETE FROM wishlist" in args[0]
        assert "inscryption" in args[1]
        
        assert view.is_on_wishlist is False
        mock_interaction.response.edit_message.assert_called_once()
        mock_interaction.followup.send.assert_called_once()

@pytest.mark.asyncio
async def test_deal_command_redirection():
    mock_interaction = MockInteraction()
    mock_db = MagicMock()
    mock_cursor = AsyncMock()
    
    # 1. First fetch: alert_channel_id (returns 999)
    # 2. Second fetch: wishlist status check (returns None/False)
    mock_cursor.fetchone.side_effect = [[999], None]
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(mock_cursor)
    
    mock_alert_channel = AsyncMock(spec=discord.TextChannel)
    mock_alert_channel.id = 999
    mock_alert_channel.mention = "<#999>"
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        mp.setattr(bot, 'db', mock_db)
        # Mock bot.get_channel to return our fake alert channel
        mp.setattr(bot, 'get_channel', MagicMock(return_value=mock_alert_channel))
        
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.side_effect = [MOCK_INSCRYPTION_SEARCH_RESULT, MOCK_INSCRYPTION_OVERVIEW_DATA, MOCK_INSCRYPTION_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await deal_command.callback(mock_interaction, "Inscryption")

        # Verify redirection occurred
        mock_alert_channel.send.assert_called_once()
        assert "sent the deals" in mock_interaction.original_response_content
        assert mock_alert_channel.mention in mock_interaction.original_response_content

@pytest.mark.asyncio
async def test_set_alert_channel_admin_success():
    mock_interaction = MockInteraction()
    mock_interaction.user.guild_permissions.manage_guild = True
    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = 999
    mock_channel.mention = "<#999>"
    
    mock_db = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.execute.side_effect = lambda *args, **kwargs: MockAiosqliteExecute(AsyncMock())

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(bot, 'db', mock_db)
        await set_alert_channel.callback(mock_interaction, mock_channel)

        assert mock_db.execute.call_count >= 1
        args = mock_db.execute.call_args[0]
        assert "INSERT OR REPLACE INTO guild_settings" in args[0]
        assert args[1] == (mock_interaction.guild_id, 999)
        mock_interaction.response.send_message.assert_called_once()
        assert "Search results will now be sent" in mock_interaction.response.send_message.call_args[0][0]

@pytest.mark.asyncio
async def test_set_alert_channel_no_permission():
    mock_interaction = MockInteraction()
    mock_interaction.user.guild_permissions.manage_guild = False
    mock_channel = MagicMock(spec=discord.TextChannel)
    
    await set_alert_channel.callback(mock_interaction, mock_channel)
    
    mock_interaction.response.send_message.assert_called_once()
    assert "You need 'Manage Server' permissions" in mock_interaction.response.send_message.call_args[0][0]
