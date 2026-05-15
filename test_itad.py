import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from main import fetch_itad_data, itadbot, GameSelectView, bot
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

# --- Helper for Mocking Discord Interaction ---
class MockInteraction:
    def __init__(self, user_id=123):
        self.created_at = datetime.datetime.now(datetime.timezone.utc)
        self.response = AsyncMock()
        self.followup = AsyncMock()
        self.user = MagicMock()
        self.user.id = user_id
        # This is important for the GameSelectView's author check
        self.user.__eq__.side_effect = lambda other: getattr(other, 'id', None) == self.user.id
        
        self.followup.send.side_effect = self._capture_followup
        
        self.sent_view = None
        self.original_response_content = None
        self.original_response_embed = None
        self.original_response_view = None

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
    
    # Mock fetch_itad_data for search and then for deals
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        # Mock the bot's session
        mp.setattr(bot, 'session', mock_session)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        # Search, Overview, then Prices
        mock_response.json.side_effect = [MOCK_INSCRYPTION_SEARCH_RESULT, MOCK_INSCRYPTION_OVERVIEW_DATA, MOCK_INSCRYPTION_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await itadbot.callback(mock_interaction, "Inscryption")

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

@pytest.mark.asyncio
async def test_itadbot_game_in_series_search_ac_origins():
    mock_interaction = MockInteraction()
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        # Search, Overview, then Prices
        mock_response.json.side_effect = [MOCK_AC_ORIGINS_SEARCH_RESULT, MOCK_AC_ORIGINS_OVERVIEW_DATA, MOCK_AC_ORIGINS_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await itadbot.callback(mock_interaction, "Assassin's Creed: Origins")

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

        await itadbot.callback(mock_interaction, "Assassin's Creed")

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
    
    with (
        MagicMock(spec=aiohttp.ClientSession) as mock_session,
        pytest.MonkeyPatch().context() as mp
    ):
        mp.setattr(bot, 'session', mock_session)
        
        mock_response = AsyncMock()
        mock_response.status = 200
        # 1. Search results, 2. Overview, 3. Prices
        mock_response.json.side_effect = [MOCK_AC_SERIES_SEARCH_RESULTS, MOCK_AC_ORIGINS_OVERVIEW_DATA, MOCK_AC_ORIGINS_DEALS_DATA]
        mock_session.request.return_value.__aenter__.return_value = mock_response

        await itadbot.callback(mock_interaction, "Assassin's Creed")

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
