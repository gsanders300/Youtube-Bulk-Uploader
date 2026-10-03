# YouTube Bulk Uploader

[![CI](https://github.com/gsanders300/Youtube-Bulk-Uploader/actions/workflows/ci.yml/badge.svg)](https://github.com/gsanders300/Youtube-Bulk-Uploader/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/gsanders300/Youtube-Bulk-Uploader)](https://github.com/gsanders300/Youtube-Bulk-Uploader/releases/latest)
[![License: MIT](https://img.shields.io/github/license/gsanders300/Youtube-Bulk-Uploader)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Top language](https://img.shields.io/github/languages/top/gsanders300/Youtube-Bulk-Uploader)](https://github.com/gsanders300/Youtube-Bulk-Uploader/search?l=python)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#installation)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

A command-line tool that uploads a folder of videos to your YouTube channel in one batch.
Every video is uploaded as **unlisted**. Titles, descriptions, tags, and recording dates are
filled in from each file, and you can review and correct the dates before anything is sent.

It runs on Windows, macOS, and Linux.

## Contents

- [What it does](#what-it-does)
- [Before you start](#before-you-start)
- [Installation](#installation)
- [Set up Google API credentials](#set-up-google-api-credentials)
- [First run](#first-run)
- [Uploading videos](#uploading-videos)
- [Command reference](#command-reference)
- [How metadata is generated](#how-metadata-is-generated)
- [How recording dates are chosen](#how-recording-dates-are-chosen)
- [Upload history, retries, and quota](#upload-history-retries-and-quota)
- [Where your files are stored](#where-your-files-are-stored)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## What it does

- **Finds videos** in a folder, and optionally its subfolders: `.mp4`, `.mov`, `.avi`,
  `.mkv`, `.webm`, `.m4v`, `.wmv`, `.flv`.
- **Reads recording dates** from embedded metadata with [ExifTool](https://exiftool.org/),
  falling back to the file's own date when ExifTool isn't installed.
- **Lets you review dates** in an interactive table and fix any that are wrong.
- **Generates metadata:** the title from the file name, a description with the recording time,
  and tags from words in the file name.
- **Uploads reliably:** resumable uploads, automatic retries, a local record of what's already
  been uploaded, and a clean stop when you hit your daily YouTube quota.

> [!IMPORTANT]
> The tool always requests `unlisted` privacy. Anyone with the link can watch an unlisted
> video, but it won't appear in search or on your channel page. The tool can't upload public
> videos.

## Before you start

### What you need

1. **A Google account** with a YouTube channel.
2. **A Google Cloud project** with the YouTube Data API enabled. It's free, and the steps are
   [below](#set-up-google-api-credentials).
3. **[uv](https://docs.astral.sh/uv/)**, the Python package manager this project uses. uv
   installs the right Python version for you, so you don't need to install Python yourself.
4. **[Git](https://git-scm.com/downloads)** to download the code, or download the ZIP from
   GitHub instead.
5. **ExifTool** (optional, recommended) for accurate recording dates.

### Limits on new Google Cloud projects

Google applies two restrictions to the project you create for this tool. Neither is a bug in
the tool.

- **Uploads start as private.** Google's
  [`videos.insert` documentation](https://developers.google.com/youtube/v3/docs/videos/insert)
  says videos uploaded from unverified API projects created after July 28, 2020 are restricted
  to private. Until your project passes Google's
  [YouTube API audit](https://support.google.com/youtube/contact/yt_api_form), expect your
  uploads to be private even though the tool asks for unlisted. You can change each video to
  unlisted in YouTube Studio afterward.
- **Sign-in expires every 7 days.** While your OAuth app's publishing status is **Testing**,
  Google issues
  [refresh tokens that expire after 7 days](https://developers.google.com/identity/protocols/oauth2#expiration).
  After that, the tool opens your browser to sign in again. This works automatically as long
  as your OAuth client file is in the config folder (see [First run](#first-run)).

## Installation

Run every command in this README from a terminal: PowerShell on Windows, Terminal on macOS
or Linux.

### 1. Install uv

macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen your terminal, then confirm it worked:

```bash
uv --version
```

The project needs uv 0.9.18 or later. Run `uv self update` if yours is older.

### 2. Install ExifTool (optional)

Without ExifTool, the tool uses file-system dates, which often change when files are copied
or downloaded. With it, the tool reads the date your camera or phone recorded.

| Platform | Command or steps |
| --- | --- |
| macOS (Homebrew) | `brew install exiftool` |
| Debian or Ubuntu | `sudo apt install libimage-exiftool-perl` |
| Fedora or RHEL | `sudo dnf install perl-Image-ExifTool` |
| Windows | See the steps below. |

On Windows:

1. Download the Windows executable ZIP from [exiftool.org](https://exiftool.org/).
2. Extract it. You get `exiftool(-k).exe` and an `exiftool_files` folder.
3. Rename `exiftool(-k).exe` to `exiftool.exe`.
4. Do one of these:
   - Add the folder that contains `exiftool.exe` to your `PATH`.
   - Copy `exiftool.exe` and the `exiftool_files` folder into the project folder after
     [step 3](#3-download-the-project). Keep them side by side.

Check it (if you used the project-folder option, run `uv run youtube-uploader exiftool`
after installation instead):

```bash
exiftool -ver
```

### 3. Download the project

With Git:

```bash
git clone https://github.com/gsanders300/Youtube-Bulk-Uploader.git
cd Youtube-Bulk-Uploader
```

Without Git, click **Code > Download ZIP** on the GitHub page, extract it, and `cd` into the
extracted folder (named `Youtube-Bulk-Uploader-main`).

This folder is the **project folder**. Run all the remaining commands from inside it.

### 4. Install dependencies

```bash
uv sync --locked
```

uv creates a `.venv` folder in the project and installs everything into it. You don't need
to activate it. Start every command with `uv run` instead.

Confirm the tool runs:

```bash
uv run youtube-uploader --version
```

## Set up Google API credentials

The tool talks to YouTube through the YouTube Data API v3, so you need your own OAuth client.
This takes about 10 minutes the first time. Your credentials stay on your computer.

### 1. Create a project and enable the API

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and sign in.
2. Create a new project from the project picker at the top of the page (for example,
   `youtube-bulk-uploader`), and make sure it's selected.
3. Open the [YouTube Data API v3 page](https://console.cloud.google.com/apis/library/youtube.googleapis.com).
4. Click **Enable**.

### 2. Configure the consent screen

1. Go to **Menu > Google Auth platform > Branding** and click **Get started**.
2. Enter an app name and your email as the support email, then click **Next**.
3. Under **Audience**, choose **External**, then click **Next**.
4. Enter your email as the contact address, click **Next**, accept the policy, and click
   **Create**.
5. Open **Audience**. Under **Test users**, click **Add users**, add the Google account that
   owns your YouTube channel, and click **Save**. Only test users can sign in while the app is
   in Testing.
6. Open **Data Access** and click **Add or remove scopes**. Find
   `https://www.googleapis.com/auth/youtube.upload` in the list (or paste it into **Manually
   add scopes**), select it, click **Update**, then click **Save**.

### 3. Create a desktop OAuth client

1. Go to **Menu > Google Auth platform > Clients**.
2. Click **Create client**.
3. Set **Application type** to **Desktop app**, give it a name, and click **Create**.
4. Click **Download JSON** in the dialog that appears. Google may not let you download the
   client secret again later, so do it now. The file is named something like
   `client_secret_1234567890-abc.apps.googleusercontent.com.json`.

> [!CAUTION]
> This JSON file and the `token.json` the tool creates give access to upload to your channel.
> Never commit them or share them. This repository's `.gitignore` excludes
> `client_secrets.json`, `client_secret_*.json`, and `token.json` as a safety net, but don't
> rely on it: keep these files out of Git repositories.

## First run

### 1. Find your config folder

```bash
uv run youtube-uploader doctor
```

On a fresh install this ends with `ERROR OAuth credentials missing` and
`Required setup checks failed`. That's expected. Note the **Configuration directory** line,
because that's where the client file goes. `doctor` creates the folder if it doesn't exist.

### 2. Copy your OAuth client file into it

Copy the JSON file you downloaded into the config folder and rename it to
`client_secrets.json` (note the plural `secrets`). Replace `client_secret_XXXX.json` below
with your file's real name.

Windows (PowerShell):

```powershell
Copy-Item "$HOME\Downloads\client_secret_XXXX.json" "$env:LOCALAPPDATA\youtube-bulk-uploader\client_secrets.json"
```

macOS:

```bash
cp ~/Downloads/client_secret_XXXX.json ~/Library/Application\ Support/youtube-bulk-uploader/client_secrets.json
```

Linux:

```bash
cp ~/Downloads/client_secret_XXXX.json ~/.config/youtube-bulk-uploader/client_secrets.json
```

You can delete the copy in your Downloads folder afterward.

### 3. Sign in

```bash
uv run youtube-uploader auth
```

A browser window opens. Pick the account that owns your channel. Google warns that it hasn't
verified the app, which is expected for your own app: click **Continue**, then approve upload
access. The tool saves `token.json` in the config folder, so later commands reuse it without a
browser.

### 4. Check again

```bash
uv run youtube-uploader doctor
```

It should end with `All required checks passed.`

> [!TIP]
> To keep the client file somewhere else, pass it with `--credentials PATH` to `auth` and
> `upload`. The tool doesn't copy it. If sign-in has to happen again (every 7 days in
> Testing) and the file isn't in the config folder, `upload` fails unless you pass
> `--credentials` again.

## Uploading videos

### 1. Add your videos

Put your video files in the `input` folder inside the project folder. Or leave them anywhere
and pass that folder's path to each command, in quotes if it contains spaces.

### 2. Preview

```bash
uv run youtube-uploader scan
```

This lists every video it found with its size, dates, and estimated upload time. Nothing is
uploaded and nothing is sent to YouTube.

### 3. Do a dry run

```bash
uv run youtube-uploader upload --dry-run
```

This shows the upload settings and the videos that would be uploaded. It doesn't sign in or
contact YouTube.

### 4. Upload

```bash
uv run youtube-uploader upload
```

Before uploading, the tool shows a numbered table of proposed recording dates and asks for a
selection:

- **`a`** accepts all dates and starts uploading.
- **`q`** cancels without uploading anything.
- **A file number**, such as `3`, opens that file's date editor. It lists every date ExifTool
  found in the file, numbered. Then:
  - Enter one of those numbers to use that date.
  - Enter `c` to type your own date as `YYYY-MM-DD HH:MM`, in UTC.
  - Enter `s` to keep the current date.

  You return to the table afterward, so you can edit more files before entering `a`.

Next, the tool signs in (using your saved token) and shows a live progress table. When it
finishes, it prints a summary and the URL of every uploaded video.

### Common examples

```bash
# Upload from a different folder, including subfolders
uv run youtube-uploader upload "/path/to/videos" --recursive

# Add text to the top of every description
uv run youtube-uploader upload --prefix "Family vacation 2026"

# Don't notify subscribers about each video in a large batch
uv run youtube-uploader upload --no-notify-subscribers

# Set category, language, and an extra tag on every video
uv run youtube-uploader upload --category-id 27 --default-language en --tag education

# Wait 30 seconds between uploads and write a detailed log
uv run youtube-uploader upload --delay 30 --log-file upload.log
```

> [!WARNING]
> YouTube requires you to disclose realistic altered or synthetic content
> (`--altered-content`) and paid promotions (`--paid-promotion`). Both default to "no."
> Settings apply to every video in the batch, so split videos with different answers into
> separate folders.

## Command reference

General form:

```text
uv run youtube-uploader COMMAND [DIR] [OPTIONS]
```

Add `--help` to any command for its full option list.

| Command | What it does |
| --- | --- |
| `auth` | Sign in to YouTube and save a token. |
| `doctor` | Check credentials, folders, and ExifTool. |
| `scan [DIR]` | List the videos in a folder with their metadata. Creates the folder if it doesn't exist. |
| `upload [DIR]` | Review dates, then upload every video in a folder. The folder must exist. |
| `exiftool` | Show the detected ExifTool path and version, or install instructions. |
| `sync-dates [DIR]` | Set each file's file-system date to its embedded recording date. Asks before changing anything. Requires ExifTool. |

`DIR` defaults to the `input` folder in the current directory.

### `auth` options

| Option | Description |
| --- | --- |
| `-c, --credentials PATH` | OAuth client JSON file to use instead of the one in the config folder. |

### `scan` options

| Option | Description |
| --- | --- |
| `-r, --recursive` | Include subfolders. |
| `--no-exiftool` | Use file-system dates only. |
| `-s, --speed MBPS` | Upload speed in megabits per second, used only for the time estimate. Default `10.0`. |

### `upload` options

| Option | Default | Description |
| --- | --- | --- |
| `-r, --recursive` | off | Include subfolders. |
| `-n, --dry-run` | off | Show what would be uploaded without uploading. |
| `-d, --delay SECONDS` | `0` | Wait between uploads. |
| `-p, --prefix TEXT` | none | Text added before each description. |
| `-l, --log-file PATH` | none | Write a detailed log to a file. |
| `-c, --credentials PATH` | config folder | OAuth client JSON file to use. |
| `--no-exiftool` | off | Use file-system dates only. |
| `-s, --speed MBPS` | `10.0` | Upload speed used only for the time estimate. |
| `--audience` | `not-made-for-kids` | `not-made-for-kids` or `made-for-kids`. |
| `--altered-content / --no-altered-content` | no | Disclose realistic altered or synthetic content. |
| `--paid-promotion / --no-paid-promotion` | no | Disclose paid product placement or endorsement. |
| `--notify-subscribers / --no-notify-subscribers` | notify | Send new-video notifications to subscribers. |
| `--allow-embedding / --no-embedding` | no embedding | Allow playback on other websites. |
| `--public-stats / --no-public-stats` | shown | Show extended statistics on the watch page. |
| `--license` | `youtube` | `youtube` (standard license) or `creative-common` (Creative Commons Attribution). |
| `--category-id N` | `22` | Numeric YouTube category ID. See the table below. |
| `--default-language CODE` | channel default | Language of the title and description, as a BCP-47 code such as `en` or `fr-CA`. |
| `--tag TEXT` | none | Add a tag to every video. Repeat for more tags. |
| `--auto-tags / --no-auto-tags` | on | Generate tags from file names. |
| `-cs, --chunk-size MB` | `64` | Upload chunk size in MB, 1 to 1024. Larger chunks mean fewer requests but more data to resend after a dropped connection. |
| `--state-file PATH` | data folder | Use a different upload-history database. |
| `--force` | off | Upload a file again even if this exact version was already uploaded. |

Common category IDs:

| ID | Category | ID | Category |
| --- | --- | --- | --- |
| 1 | Film & Animation | 22 | People & Blogs |
| 2 | Autos & Vehicles | 23 | Comedy |
| 10 | Music | 24 | Entertainment |
| 15 | Pets & Animals | 25 | News & Politics |
| 17 | Sports | 26 | Howto & Style |
| 19 | Travel & Events | 27 | Education |
| 20 | Gaming | 28 | Science & Technology |

### `sync-dates` options

| Option | Description |
| --- | --- |
| `-r, --recursive` | Include subfolders. |

> [!CAUTION]
> `sync-dates` changes file dates on disk. It sets the modification time, and the creation
> time where the operating system allows it. Review the proposed changes before you confirm.

## How metadata is generated

| YouTube field | Source |
| --- | --- |
| Title | File name without its extension, with `-`, `_`, and `.` replaced by spaces. Max 100 characters. |
| Description | The `--prefix` text (if any), a blank line, the file name without its extension, a blank line, and a line like `Recorded (Eastern): 2026-07-01 12:00:00 EDT`. Max 5000 characters. |
| Tags | Words from the file name, minus common words, numbers, and one-letter words, plus any `--tag` values. Max 500 characters combined. |
| Recording date | The calendar date (UTC) chosen in the review step. |
| Privacy | `unlisted` (but see [Limits on new Google Cloud projects](#limits-on-new-google-cloud-projects)). |

For example, `beach-day_2026.mp4` recorded at 16:00 UTC on July 1, 2026 gets the title
`beach day 2026`, the tags `beach` and `day`, and this description:

```text
beach-day_2026

Recorded (Eastern): 2026-07-01 12:00:00 EDT
```

The recording time in the description is always shown in US Eastern time (`EST` or `EDT`).
To use a different zone, change `EASTERN_TIME` in
`src/youtube_bulk_uploader/metadata.py`.

The tool doesn't set thumbnails, playlists, captions, comment settings, age restrictions, end
screens, or monetization. Change those in YouTube Studio after the upload.

## How recording dates are chosen

With ExifTool, the tool checks these embedded tags in order and uses the first plausible one:

1. `DateTimeOriginal`
2. `CreateDate`
3. `MediaCreateDate`
4. `TrackCreateDate`
5. `CreationDate`
6. `ContentCreateDate`

Dates before 1900 or more than 2 days in the future are ignored. If no embedded date is
usable, the tool falls back to the file-system date: creation time on Windows and macOS,
modification time on Linux.

The scan and review tables show all dates in UTC. Embedded dates without a time zone are
treated as UTC, which matches how most cameras and phones store video dates. The **From**
column shows where each selected date came from: `EXIF`, `FS` (file system), or `Manual`.
In a narrow terminal, the table hides the selected-date columns. Widen the window to see them.

To see the raw values for one file:

```bash
exiftool -DateTimeOriginal -CreateDate -MediaCreateDate -TrackCreateDate -CreationDate -ContentCreateDate -FileCreateDate -FileModifyDate "/path/to/video.mp4"
```

## Upload history, retries, and quota

- **Upload history:** the tool records every upload in a local SQLite database, keyed by the
  file's full path, size, and modification time. Re-running `upload` on the same folder skips
  files that already uploaded successfully and prints their URLs, so it's safe to run again.
  If you move, rename, or edit a file, it counts as a new file and uploads again. Deleting a
  video on YouTube doesn't clear its history entry, so use `--force` to upload it again.
- **Resuming:** if an upload is interrupted (Ctrl+C, a crash, or a dropped connection), run the
  same command again. The tool resumes that file where YouTube left off if the upload session
  is still valid, or restarts that file if not. Don't edit or move the file in between.
- **Retries:** temporary network and server errors are retried up to 10 times per file with
  exponential backoff, waiting at most 60 seconds between attempts. Other errors mark that
  file as failed, and the tool moves on to the next one.
- **Quota:** Google's
  [`videos.insert` documentation](https://developers.google.com/youtube/v3/docs/videos/insert)
  (checked October 2026) lists a limit of 100 upload calls per day per Google Cloud project.
  Your project's limit may differ: check the
  [Quotas page](https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas).
  When the quota runs out, the tool stops and marks the remaining videos `quota_exceeded`.
  Run the same command the next day to continue where it left off.

## Where your files are stored

| File | What it is | Location |
| --- | --- | --- |
| `client_secrets.json` | Your OAuth client | Config folder |
| `token.json` | Your saved sign-in | Config folder |
| `uploads.sqlite3` | Upload history and resumable sessions | Data folder |

Default folders:

| Platform | Config folder | Data folder |
| --- | --- | --- |
| Windows | `%LOCALAPPDATA%\youtube-bulk-uploader` | `%LOCALAPPDATA%\youtube-bulk-uploader` |
| macOS | `~/Library/Application Support/youtube-bulk-uploader` | `~/Library/Application Support/youtube-bulk-uploader` |
| Linux | `~/.config/youtube-bulk-uploader` | `~/.local/share/youtube-bulk-uploader` |

Run `uv run youtube-uploader doctor` to see the exact paths on your computer. To use different
folders, set these environment variables before running the tool:

| Variable | Changes |
| --- | --- |
| `YOUTUBE_UPLOADER_CONFIG_DIR` | Config folder |
| `YOUTUBE_UPLOADER_DATA_DIR` | Data folder |

Older versions kept the OAuth files in a `config` folder inside the project. If
`config/client_secrets.json` or `config/token.json` exists there, the tool uses it instead of
the config folder. Both names are git-ignored.

## Troubleshooting

**`doctor` says OAuth credentials are missing.**
Copy your downloaded JSON file into the config folder as `client_secrets.json`. See
[First run](#first-run).

**Sign-in fails or says access is blocked.**
Check that the OAuth client type is **Desktop app**, the YouTube Data API v3 is enabled, and
your Google account is listed under **Audience > Test users**. Then run `auth` again.

**The browser opens for sign-in again after about a week.**
That's Google's 7-day limit for apps in Testing. Sign in again. If `upload` instead fails with
`Valid credentials file not found`, put `client_secrets.json` in the config folder or pass
`--credentials`.

**I want to sign in with a different account.**
Delete `token.json` from the config folder and run `auth` again.

**Uploaded videos are private instead of unlisted.**
Your Google Cloud project hasn't passed the YouTube API audit. See
[Limits on new Google Cloud projects](#limits-on-new-google-cloud-projects). Change the
videos to unlisted in YouTube Studio, or apply for the audit.

**ExifTool isn't found.**
Run `uv run youtube-uploader exiftool` to see what the tool detects. If ExifTool is on your
`PATH`, open a new terminal and try again. Or use `--no-exiftool` to skip it.

**A date is wrong.**
Run `scan` and check the **From** column. If it says `FS`, ExifTool is missing or the file has
no embedded date. Fix the date in the review step before you upload.

**A video was skipped.**
The upload history shows that exact file already uploaded, and the tool printed its URL. Use
`--force` only if you want a second copy on YouTube.

**The upload stopped with a quota error.**
You've hit the daily upload limit for your Google Cloud project. Run the same command the next
day.

**uv says the lockfile needs to be updated.**
Don't edit `pyproject.toml` versions by hand. Run `uv sync --locked` from the project folder.
If you changed dependencies on purpose, run `uv lock` first.

**uv fails with `invalid peer certificate: UnknownIssuer`.**
Antivirus or a corporate proxy is inspecting your HTTPS traffic. Add `--system-certs` to
`uv sync` so uv trusts your operating system's certificate store.

## Development

Install all development tools:

```bash
uv sync --locked --all-groups
```

Run the checks (the same ones CI runs):

```bash
uv run --no-sync ruff check src/ tests/
uv run --no-sync ruff format --check src/ tests/
uv run --no-sync mypy src/
uv run --no-sync pytest
uv build --clear
```

Run a single test:

```bash
uv run --no-sync pytest -k TEST_NAME
```

Add or remove dependencies with `uv add` and `uv remove` (use `--group lint`, `--group test`,
or `--group type` for development tools), so `pyproject.toml` and `uv.lock` stay in sync.

### Project layout

| Path | Purpose |
| --- | --- |
| `src/youtube_bulk_uploader/main.py` | CLI commands and the interactive tables |
| `src/youtube_bulk_uploader/auth.py` | OAuth sign-in and token storage |
| `src/youtube_bulk_uploader/exiftool.py` | Reading and writing video dates with ExifTool |
| `src/youtube_bulk_uploader/metadata.py` | Titles, descriptions, tags, and upload settings |
| `src/youtube_bulk_uploader/video_processor.py` | Finding and validating video files |
| `src/youtube_bulk_uploader/uploader.py` | Resumable uploads with retries |
| `src/youtube_bulk_uploader/state.py` | SQLite upload history |
| `tests/` | Automated tests |

## License

[MIT](LICENSE)
