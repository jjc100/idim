# Step 1: Use Python 3.9-slim as the base image
FROM python:3.9-slim

# Step 2: Install Docker CLI and Docker Compose
RUN apt-get update && apt-get install -y docker.io curl && \
    curl -L "https://github.com/docker/compose/releases/download/v2.16.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose && \
    chmod +x /usr/local/bin/docker-compose && \
    rm -rf /var/lib/apt/lists/*

# Step 3: Set the working directory in the container
WORKDIR /app

# Step 4: Copy the current directory contents into the container at /app
COPY . /app

# Step 5: Install any needed packages specified in requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 6: Make port 7838 available to the world outside this container
EXPOSE 7838

# Step 7: Define environment variable for Flask app
ENV FLASK_APP=app.py

# Step 8: Run the application
CMD ["flask", "run", "--host=0.0.0.0", "--port=7838"]
