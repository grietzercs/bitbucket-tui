# bitbucket-tui (`bbtui`)

A keyboard-driven terminal UI for **Bitbucket Server / Data Center** (REST API 1.0). You can:
browse projects and repositories (including a global repo search and favorites), branches, files and file
contents; list, inspect, diff, approve, comment on, decline and **merge** pull requests; and
**clone** over HTTPS or SSH.

It was built because Sourcetree's Remote tab spun forever against an enterprise Bitbucket
instance even though the REST API and token were fine. The design rule is therefore
**nothing may spin forever**. Every network call has timeouts and a bounded number of
retries, and shows elapsed time. Esc cancels it, and failures show a clear error with
`r` to retry.

## Stack

Python 3.9+ with [Textual](https://textual.textualize.io/) (TUI), [httpx](https://www.python-httpx.org/)
(async HTTP) and [Rich](https://rich.readthedocs.io/) (diff and syntax rendering). The macOS
system `python3` is enough, and everything installs into a project-local `.venv`. Go,
Node and Homebrew packages are not needed. Textual includes a headless test driver
(Pilot), which the test suite uses for the login → browse → PR → merge → clone flows.

## Install and run

```sh
cd bitbucket-tui
make run          # creates .venv on first run, installs, launches (passes ./.env if present)
make demo         # offline demo against a built-in fake Bitbucket server
make test         # run the test suite
```

Without `make`:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/bbtui                      # or: .venv/bin/python -m bbtui
```

`pipx install .` also works if you prefer a global `bbtui` command.

### Pointing it at a server

Type the base URL into the login screen, for example `https://bitbucket.example.com`
or any other Bitbucket Server URL. A trailing slash or a pasted `/rest/api/1.0/...` suffix
is stripped, and only `http(s)://` is accepted. You can also preset it:

```sh
.venv/bin/bbtui --url https://bitbucket.example.com --user jdoe
BITBUCKET_URL=https://bitbucket.example.com .venv/bin/bbtui
```

## Authentication

| Method | Fields | Header sent |
|---|---|---|
| **Bearer token** (default) | HTTP access token / personal access token; username optional | `Authorization: Bearer <token>` |
| **Basic** | username + password **or** username + token | `Authorization: Basic base64(user:secret)` |

**Connect** (Ctrl+S) checks `/rest/api/1.0/application-properties`, which confirms the URL
is really Bitbucket and returns its version. It then calls `/rest/api/1.0/projects?limit=1`,
which proves the credentials. The signed-in user comes from Bitbucket's `X-AUSERNAME`
response header. Errors are reported specifically:

- 401 (bad or expired token, or wrong Bearer/Basic choice)
- 403
- timeouts
- TLS verification failures, with a hint about internal CAs
- DNS or connection failures
- redirects to an SSO or login page
- non-JSON (HTML) responses

### Ways to supply credentials

1. Type them into the login form. The token field is masked.
2. Environment variables, picked up automatically or with **Ctrl+E**: `BITBUCKET_URL`,
   `BITBUCKET_AUTH` (`bearer`|`basic`), `BITBUCKET_USER`/`BITBUCKET_USERNAME`,
   `BITBUCKET_PAT`/`BITBUCKET_TOKEN`, `BITBUCKET_PASSWORD`, `BITBUCKET_CA_BUNDLE`.
3. `--env-file path/to/.env`. `make run` passes `./.env` automatically. See `.env.example`.
4. The macOS Keychain, **opt-in**: tick "Save token in macOS Keychain". It is stored under
   service `bitbucket-tui` via the `keyring` library, so the token never appears on a
   command line. Next time the token is pre-filled.

There is deliberately **no `--token` flag**, because tokens on the command line end up in shell history and `ps`.

"Remember server URL and username" writes only non-secret settings to
`~/.config/bitbucket-tui/config.json` (mode 0600). `bbtui --forget` deletes that file.
Keychain items can be removed in Keychain Access by searching for `bitbucket-tui`.

## TLS

- Verification is **on** by default. Python uses the `certifi` CA bundle.
- **Also trust macOS Keychain CAs** is on by default on macOS. It exports the CA
  certificates from the System and login keychains (read-only `security find-certificate`)
  and adds them to the trust store. This mirrors curl/Safari behavior for
  internally-issued certificates. Disable it with `--no-keychain-cas` or the TLS options
  panel.
- **Custom CA bundle**: `--ca-bundle ~/certs/corp.pem`, `BITBUCKET_CA_BUNDLE`, or the TLS
  options panel.
- **Disable verification**: `--insecure` or the checkbox. It shows loud warnings and is
  never remembered between runs. Use it only to diagnose a problem.

## Keybindings

| Where | Keys |
|---|---|
| Global | **Ctrl+Q** quit · **F1** / **?** help · **Esc** cancel the running request, else go back · Tab/Shift+Tab move focus · Enter open/select |
| Login | **Ctrl+S** connect · **Ctrl+E** load from environment · Esc cancel a connection attempt |
| Any list | ↑/↓/PgUp/PgDn (the next page loads automatically near the end) · **m** load next page · **r** reload/retry · type in the filter box (server-side; Enter applies immediately) |
| Projects | **/** global repository search and favorites · **~** my personal repositories |
| Repositories | **\*** add/remove the highlighted repository as a favorite · **Ctrl+T** switch between the All and Favorites tabs (global search only) |
| Repository | **f** files · **b** branches · **p** pull requests · **c** clone · **Backspace** up one directory · **s** cycle PR state (OPEN → MERGED → DECLINED → ALL) |
| File view | **m** load more lines |
| Pull request | **d** diff · **M** merge · **a** approve · **w** needs work · **X** decline · **n** comment · **c** clone source branch · **R** refresh · **m** more activity |
| Diff | **n**/**p** next/previous file · **x** toggle context lines (3/10) · Tab to focus and scroll the diff |
| Clone | **Ctrl+S** start · → accept path completion · Esc cancel a running clone |

## Features

- **Projects** are paged with `start`/`limit` and filtered server-side with `?name=`.
- **Repositories** in a project are paged. Filtering uses `/rest/api/1.0/repos?name=&projectname=`
  and is then narrowed to the project key. **Global repository search** uses
  `/rest/api/1.0/repos?name=`. **Personal repos** are at `~user`.
- **Favorites**: `*` on any repository list marks it. The global repository view has a
  Favorites tab that re-fetches each favorite on open. A favorite that can no longer be read
  is listed as `unavailable` so it can still be removed. Favorites are kept per server in
  `~/.config/bitbucket-tui/favorites.json` (mode 0600), separate from remembered settings, so
  `--forget` leaves them in place.
- **Paging** honors `isLastPage`/`nextPageStart`. Pages load one at a time and only on
  demand: when the cursor or scroll nears the end, or when you press `m`. Thousands of
  repos are never fetched up front.
- **Branches** support a server-side `filterText`. Selecting a branch switches the file
  browser to that ref.
- **Files** come from `/browse/{path}?at=<ref>`. Directories are paged. Files are paged
  1000 lines at a time with syntax highlighting (Pygments via Rich). Binary files are
  detected and not displayed.
- **Pull requests** can be listed by state.
  - **Details** show title, author, reviewers with APPROVED/NEEDS_WORK/UNAPPROVED status,
    source → target (including fork source repos), version, URL and a Markdown description.
  - **Merge check** (`GET /merge`) shows outcome, conflicts and vetoes.
  - **Activity** shows comments with replies, approvals and other events, and is paged.
- **Diff**: the changed-file list comes from `/changes` and is paged. Per-file diffs come
  from `/diff/{path}`, are loaded only for the highlighted file, and are rendered with +/-
  coloring, hunk headers and old/new line numbers. Binary, truncated and very large diffs
  are labelled (display is capped at 5000 lines per file).
- **Merge** (`M`): a confirmation dialog re-fetches the PR, merge check and the repo's merge
  strategies (`/settings/pull-requests`), with the default strategy preselected.
  - The merge `POST`s with the PR's current `version`, both as a query parameter and in
    the body.
  - **409 out-of-date**: the dialog explains that someone else updated the PR, reloads it,
    and asks you to confirm again.
  - **409 vetoed**: each veto and conflict is listed.
  - Timeouts are reported as "outcome unknown", with a prompt to refresh before retrying.
- **Approve, needs work, decline and comment.** Decline asks for confirmation and sends
  the `version`.
- **Clone**: HTTPS or SSH, using the repo's `links.clone`.
  - A **branch** is optional and pre-filled with the PR source branch.
  - The **destination** input has path completion and defaults to `~/dev/<slug>`.
  - It refuses a non-empty target and asks before creating missing parent directories.
  - git output is streamed live, and a clone can be cancelled with Esc.

### How HTTPS clone keeps your token out of the clone

`git clone --progress -- https://host/scm/prj/repo.git <dest>` runs with the auth header
supplied through **environment-provided git config** (`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/
`GIT_CONFIG_VALUE_n`, git ≥ 2.31) as `http.https://host/.extraHeader`. This means:

- the token is **not in argv** (`ps`), shell history, or the remote URL. Any `user@` that
  Bitbucket puts in the clone link is stripped.
- the token is **not written to `.git/config`**. The app re-checks after every clone.
- `credential.helper` is cleared for that one command, so osxkeychain is neither consulted
  nor updated, and `GIT_TERMINAL_PROMPT=0` makes auth failures fail fast instead of hanging.
- the header only goes to the Bitbucket host.

Consequence: later `git fetch`/`push` in the clone use your normal git credentials
(osxkeychain) or SSH, because the token is intentionally not stored.

### SSH clone

SSH clone uses `ssh://git@host:7999/...` with `BatchMode=yes` and `ConnectTimeout=15`, and
respects `~/.ssh/config` (IdentityFile, etc.) and any `GIT_SSH_COMMAND` you have set.

Before running git, it resolves the effective host and port with `ssh -G` and does a quick
TCP reachability check. If port 7999 is unreachable you get an immediate
"SSH port 7999 appears unreachable … use HTTPS" instead of a hang. Host-key and publickey
failures come with specific hints.

## Security notes

- Tokens are never logged. An opt-in debug log (`--debug-log FILE` or `BBTUI_DEBUG_LOG`,
  file mode 0600, gitignored by `*.log`) records method, path, status and timing.
  Authorization/Cookie headers are replaced with `***REDACTED***`, and every typed secret
  (plus its Basic base64 form) is scrubbed from every log line, error message and git
  output line.
- Token and password inputs are masked.
- `.env`, `*.log`, `*.pem`/`*.key`, venvs and caches are gitignored.
- POST requests send `X-Atlassian-Token: no-check` (Bitbucket's XSRF guard). Only GETs are
  retried automatically.

## Tests

```sh
make test     # or: .venv/bin/python -m pytest -q
```

The tests run entirely offline against `bbtui/testing/fake_server.py`, an in-memory fake
of the Bitbucket REST endpoints used here (an `httpx.MockTransport`), plus `respx`. They
cover:

- URL normalization, auth header construction and error mapping (401/403/404/409/429/5xx,
  timeouts, TLS, redirects, HTML)
- GET-only bounded retries, paging (including defensive cases), filters, browse, path
  encoding, PR endpoints, merge version and veto handling, approve/decline/comment
- redaction (including a real debug-log file check), `.env` parsing, and config never
  containing secrets
- keychain storage (in-memory backend), clone command construction, and destination checks
- a **real `git clone` over HTTP from a local server that requires the Bearer header**,
  asserting the token is not persisted
- Textual Pilot smoke tests: login errors, browse/paging/filter, files/branches/file view,
  PR list/detail/diff, the merge 409 → refresh → success flow, the vetoed merge, Esc-cancel
  and UI-timeout surfacing, clone refusal and SSH hint, and an end-to-end clone from the
  TUI

## File structure

```
bbtui/
  api/        REST client, auth headers, error mapping, paging, TLS setup, response models
  services/   Non-UI logic (git clone with token-free HTTPS and SSH preflight)
  tui/        Textual app and screens (login, browse, pull request, diff rendering, clone)
  testing/    In-memory fake Bitbucket server used by tests and `make demo`
  config.py   Settings, .env parsing, saved config and favorites
  keychain.py Opt-in macOS Keychain storage
  redact.py   Secret scrubbing for logs, errors and git output
  logsetup.py Opt-in debug log
tests/        Offline test suite (unit, fake server, Textual Pilot)
```

## Contributing

| Adding | Goes in |
|---|---|
| A Bitbucket REST call or response model | `bbtui/api/` |
| Logic with no UI (git, filesystem) | `bbtui/services/` |
| A screen or widget | `bbtui/tui/` |
| A new endpoint on the fake server | `bbtui/testing/fake_server.py` |
| A test | `tests/test_<area>.py` |

Rules:

- `api/` and `services/` never import from `tui/`.
- Every network call needs a timeout; only GETs are retried.
- Anything user-typed that is secret must pass through `redact.py` before it is logged or shown.
- Tests run offline; do not add tests that need a real server.
