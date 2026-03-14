# Devcontainer for hide-my-list (OpenClaw agent project)
# Provides Claude Code CLI, GitHub CLI, and validation tools
# No Rust toolchain — this is a conversational AI project

FROM mcr.microsoft.com/devcontainers/base:ubuntu

# Install system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Shell script validation
    shellcheck \
    # Python for YAML validation and scripting
    python3-pip python3-yaml \
    # Utilities
    bc curl jq \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js LTS (needed for Claude Code)
ARG NODE_VERSION=20
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# Install Mermaid validation dependencies (used by scripts/validate-mermaid.sh)
RUN npm install -g mermaid@11 jsdom@25 dompurify@3

# Install yamllint for workflow validation
RUN pip3 install --break-system-packages yamllint

USER vscode
WORKDIR /home/vscode
