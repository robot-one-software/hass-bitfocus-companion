#!/usr/bin/env bash
# One-off: turn existing v* tags into GitHub releases (HACS lists versions from
# releases). Future tags are released automatically by .github/workflows/release.yml.
# Run as the account that owns the repo:  gh auth switch -u robot-one-software
set -euo pipefail
cd "$(dirname "$0")/.."
repo="robot-one-software/hass-bitfocus-companion"
echo "Acting as GitHub user: $(gh api user --jq .login)"
for tag in $(git tag --list 'v*' --sort=version:refname); do
  if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
    echo "$tag: release exists"
    continue
  fi
  version="${tag#v}"
  notes="$(python3 - "$version" <<'PY'
import re, sys
tag = sys.argv[1]
text = open("CHANGELOG.md").read()
m = re.search(rf"^## {re.escape(tag)}\b.*?\n(.*?)(?=^## |\Z)", text, re.S | re.M)
print(m.group(1).strip() if m else f"Release v{tag}")
PY
)"
  gh release create "$tag" --repo "$repo" --title "$tag" --notes "$notes" --verify-tag >/dev/null
  echo "$tag: released"
done
gh release list --repo "$repo" --limit 3
