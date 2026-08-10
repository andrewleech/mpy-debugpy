#!/usr/bin/env bash
#
# Assert that every submodule pin recorded in a top-repo commit is reachable
# from a branch on the fork the top repo tells CI to clone.
#
# A pin that exists only in a local submodule clone leaves the top repo's
# working tree looking identical to a correct one, and CI cannot recover: the
# submodule checkout dies with `upload-pack: not our ref <sha>` and every job
# downstream is skipped, so the run reads as a generic red rather than as "the
# tree was never fetched".
#
# Usage: tools/check-submodule-pins.sh [<commit-ish>]   (default HEAD)

set -u

# Git exports GIT_DIR (and friends) to its hooks, where they name the *top*
# repo; leaving them set would point every `git -C <submodule>` here instead.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

commit=${1:-HEAD}
top_dir=$(git rev-parse --show-toplevel) || exit 2
cd "$top_dir" || exit 2

# git@host:owner/repo.git, https://host/owner/repo.git and ssh://... all name
# the same remote; compare on the host/owner/repo tail only.
canonical_url() {
    printf '%s' "$1" |
        sed -e 's#^[a-z+]*://##' -e 's#^[^@/]*@##' -e 's#:#/#' -e 's#\.git$##' -e 's#/$##'
}

failures=0

paths=$(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}')
if [ -z "$paths" ]; then
    echo "no submodules recorded in .gitmodules" >&2
    exit 2
fi

for path in $paths; do
    name=$(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' |
        awk -v p="$path" '$2 == p {print $1}' | sed -e 's/^submodule\.//' -e 's/\.path$//')
    want_url=$(git config -f .gitmodules --get "submodule.${name}.url")
    pin=$(git ls-tree "$commit" "$path" | awk '{print $3}')

    if [ -z "$pin" ]; then
        echo "FAIL $path: no submodule pin recorded at $commit" >&2
        failures=$((failures + 1))
        continue
    fi

    remote=""
    for candidate in $(git -C "$path" remote); do
        if [ "$(canonical_url "$(git -C "$path" remote get-url "$candidate")")" = \
             "$(canonical_url "$want_url")" ]; then
            remote=$candidate
            break
        fi
    done

    if [ -z "$remote" ]; then
        echo "FAIL $path: no remote matches the .gitmodules URL $want_url" >&2
        failures=$((failures + 1))
        continue
    fi

    # Remote-tracking refs go stale the moment someone else pushes, and a stale
    # ref is exactly the false pass this check exists to prevent.
    git -C "$path" fetch --quiet "$remote" || {
        echo "FAIL $path: could not fetch $remote" >&2
        failures=$((failures + 1))
        continue
    }

    branches=$(git -C "$path" branch -r --contains "$pin" --list "$remote/*" 2>/dev/null)
    if [ -z "$branches" ]; then
        echo "FAIL $path: pin $pin is on no branch of $remote ($want_url)." >&2
        echo "     Push the submodule branch before pushing the top repo." >&2
        failures=$((failures + 1))
        continue
    fi

    echo "ok   $path: $pin on$(printf '%s' "$branches" | tr -s ' \n' ' ')"
done

if [ "$failures" -gt 0 ]; then
    echo "$failures submodule pin(s) unreachable from the fork" >&2
    exit 1
fi
