#!/usr/bin/env bash
# Recover a deleted/modified file from the Obsidian vault git history.
# Usage: ./bin/recover_vault_file.sh [search_pattern]
# Example: ./bin/recover_vault_file.sh "some-filename"
# Vault is at /opt/vault on the VPS.

VAULT_DIR="${VAULT_DIR:-/opt/vault}"
PATTERN="${1:?usage: recover_vault_file.sh <search_pattern>}"

echo "=== Vault git history recovery ==="
echo "Vault: $VAULT_DIR"
echo "Searching for files matching: *${PATTERN}*"
echo ""

# Find any commits that touched a file matching the pattern
echo "--- git log (all history, full) ---"
git -C "$VAULT_DIR" log --all --full-history --oneline -- "*${PATTERN}*"

echo ""
echo "--- Files found matching pattern ---"
# List all file paths that ever existed matching the pattern
git -C "$VAULT_DIR" log --all --full-history --diff-filter=D --name-only --format="" -- "*${PATTERN}*" | sort -u
git -C "$VAULT_DIR" log --all --full-history --name-only --format="" -- "*${PATTERN}*" | sort -u

echo ""
echo "--- Attempting to recover most recent version ---"

# Get the most recent commit hash that touched the file
COMMIT=$(git -C "$VAULT_DIR" log --all --full-history --format="%H" -- "*${PATTERN}*" | head -1)

if [ -z "$COMMIT" ]; then
  echo "ERROR: No commits found for pattern '*${PATTERN}*'"
  echo "Trying case-insensitive broader search..."
  git -C "$VAULT_DIR" log --all --full-history --oneline --name-only | grep -i "$PATTERN" | head -20
  exit 1
fi

echo "Most recent commit: $COMMIT"

# Get the exact filename from that commit
FILENAME=$(git -C "$VAULT_DIR" show --name-only --format="" "$COMMIT" -- "*${PATTERN}*" | head -1)

if [ -z "$FILENAME" ]; then
  # Try listing tree at that commit
  FILENAME=$(git -C "$VAULT_DIR" ls-tree -r --name-only "$COMMIT" | grep -i "$PATTERN" | head -1)
fi

if [ -z "$FILENAME" ]; then
  echo "ERROR: Could not determine filename at commit $COMMIT"
  echo "Files changed in that commit:"
  git -C "$VAULT_DIR" show --name-only --format="" "$COMMIT"
  exit 1
fi

echo "Filename: $FILENAME"
echo ""
echo "================================================================"
echo "FULL FILE CONTENT (from commit $COMMIT):"
echo "================================================================"
git -C "$VAULT_DIR" show "${COMMIT}:${FILENAME}"
echo ""
echo "================================================================"
echo "END OF FILE"
echo "================================================================"
