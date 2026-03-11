#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "Setting up Docker's apt repository..."

# Add Docker's official GPG key:
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources:
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update

echo "Installing Docker packages..."
# Install the latest version
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "Adding user '$USER' to the docker group..."
# Create the docker group if it doesn't exist
sudo groupadd docker || true
# Add your user to the docker group
sudo usermod -aG docker $USER

echo "========================================="
echo "Docker installation completed successfully!"
echo "IMPORTANT: You need to log out and log back in so that your group membership is re-evaluated."
echo "Alternatively, you can run: newgrp docker"
echo "========================================="
