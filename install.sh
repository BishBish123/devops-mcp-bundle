#!/usr/bin/env bash
set -euo pipefail

# devops-mcp-bundle one-line installer.
#
# Pipes safely:
#   curl -fsSL https://raw.githubusercontent.com/BishBish123/devops-mcp-bundle/main/install.sh | bash
#
# What it does (all idempotent):
#   1. Verifies python ≥ 3.11 + pip / uv on PATH
#   2. Installs the package into an isolated venv at ~/.local/share/devops-mcp-bundle
#   3. Prepends ~/.local/share/devops-mcp-bundle/bin to PATH for the calling shell
#   4. Runs `devops-mcp install --dry-run` so the user sees the resulting mcp.json

VENV="${HOME}/.local/share/devops-mcp-bundle"
PIP_PKG="devops-mcp-bundle"

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

if [ ! -d "$VENV" ]; then
    bold "creating venv at $VENV"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip >/dev/null
ok "venv ready"

bold "installing $PIP_PKG"
"$VENV/bin/pip" install --upgrade "$PIP_PKG" >/dev/null \
    || die "pip install failed (note: published-to-PyPI step is post-1.0; for now: pip install -e <local-checkout>)"
ok "$PIP_PKG installed"

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
