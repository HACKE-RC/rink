# rink

A small CLI that uploads a file or folder to a Cloudflare R2 bucket and prints a
shareable link. R2 is S3-compatible, so `rink` talks to it with `boto3`.

R2 objects are **private by default**, so `rink` gives you two kinds of link:

- **Presigned** (default) — a signed, self-expiring URL (up to 7 days). No bucket
  config needed.
- **Public** — a permanent `https://pub-xxxx.r2.dev/<key>` (or custom-domain) URL,
  available after you enable public access on the bucket.

## One-time Cloudflare setup

1. **Account ID** — Cloudflare dashboard → R2 → copy the Account ID.
2. **Create a bucket** — dashboard → R2 → *Create bucket*, or
   `wrangler r2 bucket create <name>`.
3. **API token** — dashboard → R2 → *Manage R2 API Tokens* → *Create API Token*
   with **Object Read & Write**. Copy the **Access Key ID** and **Secret Access Key**.
4. *(public links only)* bucket → Settings → enable **Public Development URL**, and
   copy the `pub-xxxx.r2.dev` domain.

## Install

From PyPI:

```sh
uv tool install rink     # install as a CLI on your PATH (recommended)
uv pip install rink      # or into the active environment
pip install rink         # or with plain pip
```

From source (for development):

```sh
uv sync                 # install deps into the project venv
uv run rink --help      # run from the project
uv tool install .       # install this checkout as a tool on your PATH
```

## Configure

```sh
rink config             # interactive wizard
```

Writes `~/.config/rink/config.toml` (mode 600). Every field can also be supplied via
environment variables, which override the file:
`RINK_ACCOUNT_ID`, `RINK_ACCESS_KEY_ID`, `RINK_SECRET_ACCESS_KEY`, `RINK_BUCKET`,
`RINK_PUBLIC_BASE_URL`.

## Buckets

```sh
rink buckets            # list all buckets in the account (default is marked ●)
rink use                # interactive picker to choose the default bucket
rink use my-bucket      # set the default bucket directly
```

`rink up --bucket <name>` still overrides the bucket for a single upload without
changing the default.

## Usage

```sh
rink up ./report.pdf                  # presigned link (default expiry)
rink up ./report.pdf --expiry 86400   # 1-day presigned link
rink up ./report.pdf --public         # permanent public link
rink up ./mydir                       # zip the folder, one link (default)
rink up ./mydir --recursive           # upload each file, one link per file
rink up ./big.bin --prefix backups/   # store under a key prefix
rink up ./f.txt --bucket other-bucket # override the configured bucket
```

Options:

| flag | default | meaning |
|------|---------|---------|
| `--public` / `--presigned` | `--presigned` | link type |
| `--expiry <sec>` | config `default_expiry` | presigned lifetime (≤ 604800) |
| `--zip` / `--recursive` | `--zip` | folder handling |
| `--prefix <str>` | none | key prefix in the bucket |
| `--bucket <name>` | configured bucket | override target bucket |

Large files (≥ 8 MiB) upload via multipart automatically with a progress bar. The
URL(s) are printed on their own line(s) so they're easy to copy or pipe.

## Listing, expiry, and deleting

```sh
rink ls                 # list objects in the bucket with each link's time-left
rink ls backups/        # only keys under a prefix
rink ls --expired       # only entries whose presigned link has already expired
rink rm myfile.zip      # delete an object (this is how you revoke access)
rink rm a.txt b.txt -y  # delete several, skip the confirmation
```

`rink ls` reads the live bucket and joins it with a local log to show the **link**
column:

- `2h` / `1d 3h` — time left on the presigned link
- `permanent` — shared via a public link (never expires)
- `untracked` — object exists in the bucket but wasn't uploaded by `rink`, so we
  don't know its link expiry

**Why a local log?** R2 stores *files*, not links. A presigned URL's expiry is baked
into the URL string at generation time — R2 keeps no record of it. So `rink` logs each
upload to a small SQLite database at `~/.local/share/rink/rink.db` (stdlib, no extra
deps) to report time-left. Deleting with `rink rm` removes both the object and its log
row.

Note: the **file itself never expires** — only the share link does. To make a file
unreachable, delete it with `rink rm`. (R2 has no per-object private/public switch;
publicity is bucket-wide, so deletion is the way to revoke.)
