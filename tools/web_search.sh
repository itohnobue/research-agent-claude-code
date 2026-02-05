#!/bin/bash
# Wrapper script for web_research.py using uv

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "Installing uv..." >&2
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env to get uv in PATH for this session
    export PATH="$HOME/.local/bin:$PATH"
fi

# Run with inline dependencies (defined in web_research.py)
# Unset PYTHONPATH to avoid conflicts with system Python packages
env -u PYTHONPATH PYTHONIOENCODING=utf-8 uv run "$SCRIPT_DIR/web_research.py" "$@"