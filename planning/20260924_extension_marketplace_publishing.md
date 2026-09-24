# Publishing the VS Code extension to the Marketplace

Date: 2026-09-24
Top repo HEAD: 1456169 (before this note)
micropython: 7c8dd9c90e (mpy-debugpy)
micropython-lib: fdfd26ae (mpy-debugpy)

Status: PLAN. The extension packages cleanly, and packaging is scripted. The
account and credential steps below need the owner. Nothing is published yet.

## Where it stands

- `extension/` builds a VSIX with `npm ci && npm run package`: 25 files,
  56 KB. It carries only the compiled `out/` (no tests or maps), `smol-toml`'s
  runtime `dist/`, `LICENSE`, and a Marketplace-facing `README.md` and
  `CHANGELOG.md`. `.vscodeignore` sets what is left out. The 98 unit tests
  pass. The VSIX installs into VS Code (checked on the WSL server).
- `package.json` has `publisher: andrewleech`, `preview: true`, `repository`,
  `homepage`, `bugs` and `keywords`. There is also a `vscode:prepublish`
  hook, so `vsce publish` always compiles first. `@vscode/vsce` ^3.2 is a
  devDependency, pinned by the lockfile.
- Marketplace, checked 2026-09-24: `andrewleech.mpy-debugpy` does not exist,
  and the gallery lists no extensions under publisher `andrewleech`. Whether
  that publisher ID is already registered to someone cannot be told from the
  public API. The display name "MicroPython Debug" is not taken. The nearest is
  `ghielectronics.micropython-debugger`, "MicroPython Debugger".
- Open VSX has `ms-python.debugpy` (2026.6.0), which this extension declares
  in `extensionDependencies`. So publishing there too (for VSCodium, Cursor
  and the like) would install cleanly.

## The blocker is not the Marketplace

A Marketplace user installs the extension and then needs three things it
cannot provide:

1. an `mpremote` with the `debug` command. It exists only on
   `andrewleech/micropython@mpy-debugpy` (`tools/mpremote`), not in any
   release;
2. firmware with `sys.settrace`. The project's CI publishes it as GitHub
   Releases (`fw-<sha>`), for four boards;
3. `debugpy` on the board. It comes from the micropython-lib fork, through
   `mpremote debugpy-install <dir>`, which needs a local copy of the package.

So today the extension is only usable by someone who has cloned this repo.
Its README says so up front, and `preview: true` marks it. Publishing now
buys discoverability, and a stable install path for people who follow the
repo's README. Anyone who finds it cold hits requirement 1 at once, with the
error `failed to start mpremote`, or mpremote's own "no such command".

Options, in order of how much they fix:

- **A. Publish now as preview**, requirements stated (the current state).
  Cheap. The cost is first impressions and support questions.
- **B. First make `mpremote` installable from the fork**, then publish.
  `uv tool install "git+https://github.com/andrewleech/micropython@mpy-debugpy#subdirectory=tools/mpremote"`
  already works. Measured 2026-09-24 after the push: it installs
  `mpremote 1.29.0.post45+g7c8dd9c90` in 4.5 minutes (most of that is
  cloning micropython), and `mpremote debug -t unix target:main` from that
  install prints its `MPDBG-READY` line. So what B still needs is on the
  extension side: detect a missing `debug` subcommand and link the install
  instructions. Removes requirement 1 for anyone with Python.
- **C. Wait for upstream**: `mpremote debug` in a released mpremote, and
  `debugpy` installable with `mip`. Everything becomes stock, but the timing
  is not ours.

Recommended: **B, then publish.** The extension-side check is small, and the
install line is proven.

## Steps to publish (owner actions marked)

1. **(owner)** Sign in at <https://marketplace.visualstudio.com/manage> with a
   Microsoft account, and create publisher `andrewleech`. If the ID is taken,
   pick another and change `publisher` in `package.json`: the extension ID
   becomes `<publisher>.mpy-debugpy`, and the repo's docs never name it.
2. **Icon.** The Marketplace wants a PNG of at least 128x128. The publishing
   rules reject SVGs, both as the icon and in the README. Add
   `extension/images/icon.png` and `"icon": "images/icon.png"`. Nothing in
   the repo can stand in for one, so it needs making.
3. **Credential.** Azure DevOps retires global PATs on 2026-12-01, and the
   PAT `vsce publish` has used is one of those. So set up the path the VS Code
   docs now recommend, not a PAT that dies in ten weeks:
   - **(owner)** Create a Microsoft Entra app registration. Give it a
     federated credential for this repo's GitHub environment, for example
     `vscode-marketplace`.
   - **(owner)** Add that identity as a member of the Marketplace publisher.
   - Add a release workflow (`.github/workflows/extension.yml`). It triggers
     on a tag `ext-v<version>` with `permissions: id-token: write`, then:
     `npm ci`, `npm test`, `npm run package`, `azure/login` over OIDC,
     `npx vsce publish --azure-credential --packagePath <vsix>`. That flag
     needs vsce ≥ 2.26.1, and the devDependency is ^3.2. It then attaches the
     VSIX to a GitHub Release, which covers people who install by hand.
   - To publish once by hand before that, `vsce login` with an
     organization-scoped PAT (Marketplace: Manage) still works until the
     retirement.
4. **Open VSX (optional).** Claim the `andrewleech` namespace at
   open-vsx.org and create a token. Add `npx ovsx publish <vsix>` to the same
   workflow, with the token as a secret.
5. **Version and changelog.** 0.1.0 is unpublished, so the first release can
   be 0.1.0. After that, bump `version` and add a `CHANGELOG.md` entry per
   tag. The workflow should refuse a tag that does not match `version`.
6. **After publishing**: change the README's "not on the Marketplace yet"
   section and `docs/debugging.md` "Attaching from VS Code" to name the
   Marketplace install, and keep the VSIX build as the from-source route.

## Verification before the first publish

- `npx vsce ls` shows only runtime files (it does now).
- Install the published build in a clean VS Code profile, with no repo
  checkout. The missing-`debug` message must be what the user sees (option
  B), not a raw spawn error.
- The extension-host suite (`xvfb-run -a npm run test:host`, which is CI's
  `extension-host` job) passes on the release commit.
