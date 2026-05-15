# Discord Deal Bot

A Discord bot that fetches game deals and historical lows using the IsThereAnyDeal (ITAD) API v2.

## Features
- Search for games using `/itadbot`.
- View current top deals across various stores.
- View historical low prices and active bundle information.
- Supports multiple regions (US, UK, CA, AU, DE, FR).

## Setup

1. **Clone the repository:**
   ```bash
   git clone <your-repo-url>
   cd discord-deal-bot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configuration:**
   Create a `.env` file in the root directory and add your tokens:
   ```env
   DISCORD_TOKEN=your_discord_bot_token_here
   ITAD_API_KEY=your_itad_api_key_here
   ```

4. **Run the bot:**
   ```bash
   python main.py
   ```