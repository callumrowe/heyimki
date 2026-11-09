# Shared helpers for setup.sh and scripts/*. Source it; don't run it.
# Kept compatible with bash 3.2 (macOS's /bin/bash).

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
fail()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

ask() {        # ask "Prompt" default → echoes the answer
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then read -r -p "$prompt [$default]: " reply || true
  else read -r -p "$prompt: " reply || true
  fi
  printf '%s' "${reply:-$default}"
}

ask_secret() { # ask_secret "Prompt" → echoes the answer without showing it
  local reply
  read -r -s -p "$1: " reply || true
  echo >&2
  printf '%s' "$reply"
}

yes_no() {     # yes_no "Question" y|n → returns 0 for yes
  local default="$2" reply hint="[y/N]"
  [[ "$default" == "y" ]] && hint="[Y/n]"
  read -r -p "$1 $hint " reply || true
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else python3 -c "import secrets; print(secrets.token_hex(32))"
  fi
}

get_kv() {     # get_kv KEY → the value of KEY in .env (empty if unset)
  KEY="$1" awk 'BEGIN { k = ENVIRON["KEY"] } index($0, k "=") == 1 { print substr($0, length(k) + 2); exit }' .env
}

set_kv() {     # set_kv KEY VALUE — replace KEY=... in .env
  KEY="$1" VAL="$2" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"] }
    index($0, k "=") == 1 { print k "=" v; found = 1; next } { print }
    END { if (!found) print k "=" v }' .env > .env.tmp
  cat .env.tmp > .env   # rewrite in place so .env keeps its 600 permissions
  rm -f .env.tmp
}
