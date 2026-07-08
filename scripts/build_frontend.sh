#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cd frontend
npm install
npm run build

# Copy to source tree
rm -rf ../onebot_adapter/webui/static/*
cp -r dist/* ../onebot_adapter/webui/static/
echo "WebUI built -> onebot_adapter/webui/static/"

# Also copy to site-packages if the package is pip-installed (not editable)
# so the runtime finds the built SPA without rebuilding site-packages.
PACKAGE_DIR=$(python3 -c "import onebot_adapter; print(onebot_adapter.__path__[0])" 2>/dev/null || echo "")
if [ -n "$PACKAGE_DIR" ] && [ "$PACKAGE_DIR" != "../onebot_adapter" ]; then
    TARGET="$PACKAGE_DIR/webui/static"
    mkdir -p "$TARGET"
    rm -rf "$TARGET"/*
    cp -r dist/* "$TARGET/"
    echo "WebUI also copied -> $TARGET"
fi
