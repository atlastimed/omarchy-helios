#!/usr/bin/env bash
# Check the repository against Omarchy's plugin validator and the marketplace's
# extra rules, on a clean checkout: exactly one manifest.json, at the root; no
# tracked symlinks; required metadata; entry points that exist; and the plugin
# and daemon versions in step.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { printf 'check-plugin: %s\n' "$*" >&2; exit 1; }

manifests=$(git ls-files | grep -E '^([^/]+/)?manifest\.json$' || true)
[[ "$manifests" == "manifest.json" ]] || fail "expected exactly one manifest.json, at the root; found: ${manifests:-none}"

symlinks=$(git ls-files -s | awk '$1 == "120000" { print $4 }')
[[ -z "$symlinks" ]] || fail "tracked symlinks are not allowed: $symlinks"

jq -e '
  .schemaVersion == 1
  and (.id | type == "string" and test("^[a-z0-9][a-z0-9._-]*$") and (startswith("omarchy.") | not))
  and (.name | type == "string" and length > 0)
  and (.version | type == "string" and length > 0 and length <= 64)
  and (.author | type == "string" and length > 0 and length <= 120)
  and (.description | type == "string" and length > 0 and length <= 500)
  and (.kinds | type == "array" and length > 0)
  and (.entryPoints | type == "object")
' manifest.json >/dev/null || fail "manifest.json is missing a required field or has an invalid one"

# Each declared kind needs its entry point, as Omarchy's validator requires.
for kind in $(jq -r '.kinds[]' manifest.json); do
  case "$kind" in
    bar-widget) key=barWidget ;;
    panel | overlay | menu | service | bar) key=$kind ;;
    *) fail "unknown kind $kind" ;;
  esac
  jq -e --arg key "$key" '.entryPoints[$key] | type == "string"' manifest.json >/dev/null \
    || fail "kind $kind has no entryPoints.$key"
done
jq -e '(.barWidget.defaultSection // "center") | IN("left", "center", "centerBegin", "centerEnd", "right", "tray")' \
  manifest.json >/dev/null || fail "barWidget.defaultSection must be left, center, centerBegin, centerEnd, right or tray"

for entry in $(jq -r '.entryPoints[]' manifest.json); do
  [[ "$entry" != /* && "$entry" != *..* ]] || fail "entry point $entry must be a relative path inside the plugin"
  git ls-files --error-unmatch "$entry" >/dev/null 2>&1 || fail "entry point $entry is not in the repository"
done

plugin_version=$(jq -r .version manifest.json)
daemon_version=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n1)
[[ "$plugin_version" == "$daemon_version" ]] || fail "manifest.json version $plugin_version differs from pyproject.toml $daemon_version"
module_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/helios/__init__.py)
[[ "$plugin_version" == "$module_version" ]] || fail "manifest.json version $plugin_version differs from src/helios/__init__.py $module_version"

git ls-files --error-unmatch README.md LICENSE >/dev/null 2>&1 || fail "README.md and LICENSE must be at the root"

if command -v omarchy-plugin-validate >/dev/null; then
  export_dir=$(mktemp -d)
  trap 'rm -rf "$export_dir"' EXIT
  git archive HEAD | tar -x -C "$export_dir"
  omarchy-plugin-validate "$export_dir"
fi

echo "check-plugin: ok"
