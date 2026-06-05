# rink

[![PyPI version](https://img.shields.io/pypi/v/rink.svg?color=blue)](https://pypi.org/project/rink/)
[![Python versions](https://img.shields.io/pypi/pyversions/rink.svg)](https://pypi.org/project/rink/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/HACKE-RC/rink/blob/master/LICENSE)

`rink` is a fast, terminal-first utility to upload files or directories directly to a Cloudflare R2 bucket and instantly get a shareable URL.

Cloudflare R2 objects are private by default, so `rink` provides two ways to share:
- **Presigned Links (Default)**: A secure, signed URL that automatically expires (up to 7 days). Does not require public bucket access.
- **Public Links**: A permanent URL (`https://<pub-domain>/<key>`) using your bucket's public access domain.

---

## Features

- **Direct Uploads**: Single files, multiple files, folders, or piped input from standard input (`stdin`).
- **Flexible Folder Handling**: Zip directories automatically into a single file, or upload them recursively in parallel.
- **Terminal Enhancements**: Copy links directly to your clipboard (`-c`) or display scannable QR codes (`--qr`).
- **Track Expirations**: Uses a lightweight local SQLite database to track presigned URLs and their remaining lifespan.
- **Multipart Uploads**: Automatically switches to multipart uploads for files ≥ 8 MiB with smooth progress bars.
- **Self-Cleaning**: Easily list expired links and prune them from R2 to keep your storage clean.
- **Receive Links**: `rink serve` can create file-drop links backed by a tiny Cloudflare Worker in front of your bucket.

---

## Installation

Install `rink` via PyPI. We recommend using `uv` for easy tool management, but standard `pip` works too.

### Using `uv` (Recommended)
```bash
# Install as a global CLI tool on your PATH
uv tool install rink

# Or install into your active environment
uv pip install rink
```

### Using `pip` or `pipx`
```bash
pipx install rink
# or
pip install rink
```

### From Source (Development)
```bash
# Clone the repository and sync dependencies
git clone https://github.com/HACKE-RC/rink.git
cd rink
uv sync

# Run from the project
uv run rink --help

# Install the local checkout as a global tool
uv tool install .
```

---

## One-Time Cloudflare Setup

Before using `rink`, you need to retrieve your Cloudflare credentials and create a bucket:

1. **Get Account ID**: Go to the [Cloudflare Dashboard](https://dash.cloudflare.com/) → **R2** → Copy the **Account ID** from the right sidebar.
2. **Create a Bucket**: Click **Create bucket** in the dashboard, or run `wrangler r2 bucket create <name>`.
3. **Generate API Token**: 
   - Click **Manage R2 API Tokens** → **Create API Token**.
   - Select permissions: **Object Read & Write**.
   - Copy the **Access Key ID** and **Secret Access Key**.
4. *(Optional for Public Links)*: In your bucket's page → **Settings** → **Public Access** → Enable **Public Development URL** (or configure a custom domain) and copy the `pub-xxxx.r2.dev` address.

---

## Quick Start

Initialize `rink` using the interactive setup wizard:

```bash
rink config
```
This wizard will prompt you for your Account ID, Access Keys, default bucket name, and preferred link configurations.

### Configuration Storage
Your settings are saved at `~/.config/rink/config.toml` (file mode `600` for security). 

You can also use environment variables to configure `rink` or override config files:
- `RINK_ACCOUNT_ID`
- `RINK_ACCESS_KEY_ID`
- `RINK_SECRET_ACCESS_KEY`
- `RINK_BUCKET`
- `RINK_PUBLIC_BASE_URL`
- `RINK_SERVE_URL`
- `RINK_SERVE_TOKEN`

---

## Usage & Command Reference

### Uploading Files (`rink up`)
Upload one or more files/directories and print their shareable links.

```bash
# Upload a single file with default expiry (signed URL)
rink up report.pdf

# Upload and copy the link directly to clipboard
rink up report.pdf -c

# Generate a scannable terminal QR code for the upload
rink up report.pdf --qr

# Upload with a custom expiration (e.g. 30m, 2h, 7d, 1h30m, or bare seconds)
rink up report.pdf --expiry 1d

# Upload as a permanent public link (requires public URL setup)
rink up report.pdf --public

# Pipe content directly from stdin (requires --name)
cat logs.txt | rink up - --name system_logs.txt

# Prefix files or randomize the upload paths for privacy
rink up invoice.pdf --prefix backups/
rink up secret.docx --random

# Force browsers to download the file instead of viewing it inline
rink up photo.png --download
```

#### Upload Options
| Flag | Default | Description |
| :--- | :--- | :--- |
| `--public` / `--presigned` | `--presigned` | Choose link type (public permanent or presigned temporary). |
| `--expiry <duration>` | Config default | Presigned URL duration (e.g. `30m`, `2h`, `7d`). Max `7d`. |
| `--zip` / `--recursive` | `--zip` | Folders: zip into one file, or upload contents recursively in parallel. |
| `--workers <count>` | `4` | Number of parallel upload workers for recursive directories. |
| `--prefix <string>` | None | Object key prefix path in the bucket. |
| `--bucket <name>` | Config bucket | Override the target bucket. |
| `--name <string>` | Source filename | Destination key name (required for stdin). |
| `--random` | Off | Prepend a random string to the destination key for unguessable links. |
| `--download` | Off | Forces the browser to download the file (`Content-Disposition: attachment`). |
| `--copy`, `-c` | Off | Copies the generated URL(s) to the clipboard. |
| `--qr` | Off | Prints a terminal QR code for the uploaded file link. |
| `--quiet`, `-q` | Off | Prints only the URL(s) (useful for scripting and piping). |
| `--json` | Off | Formats command output as JSON. |

---

### Listing & Managing Buckets
```bash
# List all R2 buckets in your Cloudflare account
rink buckets

# Pick a default bucket interactively
rink use

# Set default bucket directly
rink use my-bucket
```

### Listing & Regenerating Links (`rink ls`, `rink link`, `rink open`)
```bash
# List all tracked uploads with their remaining lifetime
rink ls

# Filter tracked files by bucket prefix
rink ls backups/

# Filter to show only files with expired presigned links
rink ls --expired

# Regenerate a fresh URL for an existing file (without re-uploading)
rink link invoice.pdf --expiry 7d -c

# Open an uploaded file's URL in your default web browser
rink open invoice.pdf
```

### Receiving Files (`rink serve`)
`rink serve` inverts the normal upload flow. You deploy a small Worker in front
of your R2 bucket, then generate a receive link and send it to someone else.
They upload through the browser page; the Worker streams the file into your R2
bucket without exposing your R2 credentials.

First scaffold and deploy the Worker:

```bash
# Write a deployable Worker project bound to your configured R2 bucket
rink serve --init --worker-dir rink-serve-worker

# Deploy, sync the Worker secret, and save the Worker URL/token into rink config
rink serve --deploy --worker-dir rink-serve-worker
```

`rink serve --deploy` uploads the local admin token as the Worker secret, parses
Wrangler's deployed `workers.dev` URL, and saves both values. If Wrangler does
not print a URL, rink asks for it.

Create a receive link:

```bash
# One file, expires in 1 day, 512 MiB max file size
rink serve

# More control
rink serve --label "Send me the signed PDF" --expiry 2h --max-size 50MB --prefix inbox/contracts -c

# Let the link accept three uploads
rink serve --max-uploads 3
```

Each received file gets a Worker download URL. By default those download URLs
are one-time links (`--download-views 1`), and the Worker Durable Object tracks
view counts. The receive page shows browser upload progress and offers a direct
copy button for the generated download link.

### Deleting & Revoking Access (`rink rm`, `rink prune`)
To revoke access to a file, you must delete it from the bucket.
```bash
# Delete an object and clear its tracking log
rink rm report.pdf

# Delete multiple files and skip verification prompts
rink rm image1.png image2.png -y

# Delete all R2 files whose tracked presigned links have expired
rink prune
```

---

## How It Works (Under the Hood)

Since Cloudflare R2 stores raw object files rather than temporary share links, R2 has no native concept of "when a presigned link expires". A presigned URL's expiration is computed cryptographically and embedded directly in the URL query string.

To solve this, `rink` maintains a lightweight local SQLite database at `~/.local/share/rink/rink.db`.
- Every time you perform an upload, `rink` logs the object key, bucket name, and link expiration timestamp locally.
- Running `rink ls` joins live bucket object information with this local log to determine exactly how much time is left on each link.
- Objects uploaded outside of `rink` (or whose local log records are missing) will show up as `untracked`.
- Deleting an object via `rink rm` deletes it from R2 and removes its tracking entry from your database.

> [!NOTE]
> Presigned link expiration **does not delete the file** from R2; it only makes the URL invalid. To save bucket space, run `rink prune` to delete expired files from your bucket.

---

## Development

Contributions are welcome! To set up your local development environment:

1. Clone the repository and install dev dependencies:
   ```bash
   uv sync --dev
   ```
2. Run tests (we use `moto` to mock S3/R2 requests locally; no live network calls are made):
   ```bash
   uv run pytest -v
   ```
3. Run tests quietly:
   ```bash
   uv run pytest -q
   ```

---

## License

This project is licensed under the [MIT License](LICENSE).
