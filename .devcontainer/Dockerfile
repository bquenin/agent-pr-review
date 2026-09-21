FROM mcr.microsoft.com/devcontainers/base:3-ubuntu24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 jq shellcheck tmux \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/home/vscode/.local/bin:${PATH}"
