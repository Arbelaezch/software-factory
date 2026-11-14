# Claude account switching for harness projects (zsh or bash). Add this line to ~/.zshrc:
#
#   source ~/code/software-factory/scripts/claude-accounts.sh
#
# Then `claude` started inside a project whose .claude/harness.toml says
#   [accounts]
#   claude = "personal"
# runs with that account's config directory (set in scripts/defaults.toml under [accounts.dirs]).
# Each directory keeps its own login, so both accounts stay signed in. Outside harness projects,
# or when you set CLAUDE_CONFIG_DIR yourself, `claude` behaves exactly as before.
#
# Also adds `claude-as <account>`, `claude-work` and `claude-personal` to pick one explicitly, and
# `harness-setup` to set up the harness in a new project folder.

if [ -n "${ZSH_VERSION:-}" ]; then
  eval '_HARNESS_HOME="${${(%):-%x}:A:h:h}"'
else
  _HARNESS_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

_harness_run_claude() {  # $1 = "name|dir" ; rest = args
  local spec="$1"; shift
  local name="${spec%%|*}" dir="${spec#*|}"
  if [ -n "$dir" ]; then
    mkdir -p "$dir" && chmod 700 "$dir" 2>/dev/null
    printf 'claude: using the %s account (%s)\n' "$name" "$dir" >&2
    CLAUDE_CONFIG_DIR="$dir" command claude "$@"
  else
    printf 'claude: using the %s account (~/.claude)\n' "$name" >&2
    env -u CLAUDE_CONFIG_DIR claude "$@"
  fi
}

claude() {
  if [ -z "${CLAUDE_CONFIG_DIR:-}" ]; then
    local spec
    spec="$(python3 "$_HARNESS_HOME/scripts/harness.py" account 2>/dev/null)"
    if [ -n "$spec" ]; then
      _harness_run_claude "$spec" "$@"
      return
    fi
  fi
  command claude "$@"
}

claude-as() {
  local spec
  spec="$(python3 "$_HARNESS_HOME/scripts/harness.py" account --dir-for "$1")" || return 1
  shift
  _harness_run_claude "$spec" "$@"
}
claude-work() { claude-as work "$@"; }
claude-personal() { claude-as personal "$@"; }

# Set up the harness in the current folder. The plugin is installed per project, so a new folder doesn't have
# /harness-init yet: this runs it in a one-off session that loads the plugin straight from this repo. At the end
# /harness-init installs the plugin for the project's account and closes the session ($HARNESS_SETUP_PID).
harness-setup() {
  sh -c 'HARNESS_SETUP_PID=$$ exec claude --plugin-dir "$0" "/harness:harness-init"' "$_HARNESS_HOME"
  if [ -f .claude/harness.toml ]; then
    printf 'harness-setup: done. Start `claude` here (it picks the project'"'"'s account) to use /ship and /factory.\n' >&2
  fi
}
