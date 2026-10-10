FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the lockfile. --require-hashes is implied
# by a fully hashed requirements.txt, but spelling it out means a hand-edited
# line without a hash fails the build instead of silently installing whatever
# PyPI serves that day (requirements.in explains why this project locks).
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy application
COPY . .

# Expose port
EXPOSE 8080

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
