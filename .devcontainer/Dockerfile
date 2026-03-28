FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        pciutils \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspaces/ansible-collection-granzer04-gpu-facts
