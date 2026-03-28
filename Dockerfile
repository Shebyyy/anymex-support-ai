FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY app.py .
COPY templates/ templates/

EXPOSE 8080

# Start Flask dashboard in background, then run the bot
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --daemon && python bot.py
