#!/usr/bin/env bash
set -euo pipefail

# devops-mcp-bundle one-line installer.
#
# Pipes safely:
#   curl -fsSL https://raw.githubusercontent.com/BishBish123/devops-mcp-bundle/main/install.sh | bash
#
# What it does (all idempotent):
#   1. Verifies python ≥ 3.11 on PATH
#   2. Installs the package into an isolated venv at ~/.local/share/devops-mcp-bundle
#   3. Symlinks the four console scripts into ~/.local/bin
#   4. Tells the user how to wire everything into Claude Code's mcp.json
#
# The package is installed from the GitHub repo until v1.0 lands on
# PyPI. Override with PIP_SOURCE=<spec> to install from a fork, a tag,
# or a local checkout.

VENV="${HOME}/.local/share/devops-mcp-bundle"
PIP_SOURCE="${PIP_SOURCE:-git+https://github.com/BishBish123/devops-mcp-bundle.git}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n'  "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n'  "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n'  "$*" >&2; exit 1; }

bold "devops-mcp-bundle installer"

command -v python3 >/dev/null || die "python3 not found"
PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYV" in
    3.11|3.12) ok "python ${PYV}";;
    *) die "need python 3.11 or 3.12, found ${PYV}";;
esac

# A previous run may have left a half-built venv (e.g. ^C between
# `python -m venv` and `pip install`). Treat the directory's existence
# as a hint, not proof — if the interpreter or pip is missing we
# rebuild from scratch rather than try to repair-in-place.
if [ -d "$VENV" ] && { [ ! -x "$VENV/bin/python" ] || [ ! -x "$VENV/bin/pip" ]; }; then
    warn "found a partial/broken venv at $VENV (missing python or pip); recreating"
    rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
    bold "creating venv at $VENV"
    python3 -m venv "$VENV"
fi
[ -x "$VENV/bin/python" ] || die "venv at $VENV is missing bin/python after creation"
[ -x "$VENV/bin/pip" ]    || die "venv at $VENV is missing bin/pip after creation"
"$VENV/bin/pip" install --upgrade pip >/dev/null
ok "venv ready"

bold "installing devops-mcp-bundle from $PIP_SOURCE"
"$VENV/bin/pip" install --upgrade "$PIP_SOURCE" >/dev/null \
    || die "pip install failed — try PIP_SOURCE=<your-spec> $0 (e.g. a local checkout, a tag, or a fork)"

# Smoke-check the install: the entry point has to actually run. A pip
# install can succeed with a missing dependency or a broken script
# shebang; catching that here is cheaper than a confused user later.
if ! "$VENV/bin/devops-mcp" version >/dev/null 2>&1; then
    die "post-install smoke check failed: $VENV/bin/devops-mcp version did not run cleanly"
fi
ok "devops-mcp-bundle installed"

LINK_DIR="${HOME}/.local/bin"
mkdir -p "$LINK_DIR"
for cmd in mcp-postgres-dba mcp-k8s-inspector mcp-observability devops-mcp; do
    ln -sf "$VENV/bin/$cmd" "$LINK_DIR/$cmd"
done
ok "symlinked CLIs into $LINK_DIR (add to PATH if not already)"

case ":$PATH:" in
    *":$LINK_DIR:"*) ;;
    *) warn "$LINK_DIR is not on \$PATH — add it to your shell rc";;
esac

bold "done. Next steps:"
cat <<EOF

  1. Set the env vars you need:
       export POSTGRES_DSN=postgresql://user:pass@host:5432/db
       export KUBECONFIG=\$HOME/.kube/config
       export PROMETHEUS_URL=http://localhost:9090
       export LOKI_URL=http://localhost:3100

  2. Wire the bundle into Claude Code:
       devops-mcp install --pgvector-dsn "\$POSTGRES_DSN" \\
                          --prometheus-url "\$PROMETHEUS_URL" \\
                          --loki-url "\$LOKI_URL" \\
                          --kubeconfig "\$KUBECONFIG"

  3. Restart Claude Code. The three servers should now show up in
     'Manage MCP Servers'.

EOF
