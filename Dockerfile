FROM python:3.11-slim

WORKDIR /app

# Ensure logs are flushed to the terminal immediately
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Command to run the bot
# Note: We assume DISCORD_TOKEN and ITAD_API_KEY 
# are provided as Environment Variables by the host
ENTRYPOINT ["python3", "main.py"]