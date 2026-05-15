FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create a directory for the database volume mount
RUN mkdir -p /data

CMD ["python", "main.py"]