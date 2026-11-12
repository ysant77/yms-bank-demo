#!/bin/sh
# Full-history leak scan. Run before the repository is made public.
set -eu
cd "$(git rev-parse --show-toplevel)"
echo "gitleaks: all commits on all branches"
gitleaks git --redact --no-banner --log-opts="--all"
if [ -f .private-names ]; then
  echo "private names: all commits on all branches"
  if git log --all -p | grep -i -F -f .private-names >/dev/null; then
    echo "FOUND a name from .private-names in history" >&2
    exit 1
  fi
  if git log --all --format='%an %ae %cn %ce %B' | grep -i -F -f .private-names >/dev/null; then
    echo "FOUND a name from .private-names in commit metadata" >&2
    exit 1
  fi
fi
echo "history scan: clean"
