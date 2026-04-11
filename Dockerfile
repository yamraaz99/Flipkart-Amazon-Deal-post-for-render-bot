# Use a lightweight Python image
FROM python:3.10-slim

# Install WeasyPrint's required OS dependencies
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Set up the working directory
WORKDIR /app

# Copy your requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your files (main.py, keep_alive.py)
COPY . .

# Expose port 8080 for Render's web traffic
EXPOSE 8080

# This is your Start Command!
CMD ["python", "main.py"]
