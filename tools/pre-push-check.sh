#!/usr/bin/env bash
#
# git pre-push hook. Installed by `make bootstrap` as .git/hooks/pre-push.
#
# Reads the standard pre-push stdin format:
#   <local ref> <local oid> <remote ref> <remote oid>
# and refuses the push if any commit being sent names a submodule pin that is
# not reachable on the fork CI clones from. Bypass with `git push --no-verify`.

set -u

# Installed as a symlink from .git/hooks, so resolve the sibling script through
# it rather than off the worktree root, which a git worktree would not share.
tools_dir=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
status=0

while read -r _local_ref local_oid _remote_ref _remote_oid; do
    # An all-zero local oid is a branch deletion: nothing is being sent.
    case "$local_oid" in
        *[!0]*) ;;
        *) continue ;;
    esac
    if ! "$tools_dir/check-submodule-pins.sh" "$local_oid"; then
        status=1
    fi
done

if [ "$status" -ne 0 ]; then
    echo "pre-push: refusing to push (see above). Override with --no-verify." >&2
fi

exit "$status"
