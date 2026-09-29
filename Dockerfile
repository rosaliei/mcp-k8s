# One image for all the long-running Python services (sample-app and graphql-api).
# docker-compose.yml builds it once and decides which script each container runs (the "command:").

# Start from the official slim Python image (Debian + Python 3.12, no extras).
FROM python:3.12-slim

# All following commands run inside /app in the image.
WORKDIR /app

# Print logs immediately instead of buffering them, so `docker compose logs` shows them right away.
ENV PYTHONUNBUFFERED=1

# Copy requirements FIRST and install them. Docker caches each step ("layer"):
# as long as requirements.txt doesn't change, this slow step is reused on the next build,
# even if you change the Python code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the code (.dockerignore keeps .venv, .git etc. out of the image).
COPY . .
