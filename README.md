# insdl

A small Instagram downloader for local use on Termux/Android.

## Features

- Download Instagram `/p/`, `/reel/`, `/tv/` links to local storage
- Prefer local Chromium login-state decryption
- Supports `Chrome / Chromium / Edge / Brave / Arc`
- Cookie source priority: env / `.env` / `scripts/cookie.txt` / local Chromium cookie DB
- Error classification + one retry for network/rate-limit paths
- Atomic downloads via `.part` → rename

## Usage

```bash
python scripts/ig.py --auth-check
python scripts/ig.py --url "https://www.instagram.com/p/<shortcode>/"
```

Default output directory:

- `/sdcard/Pictures/Instagram/{shortcode}/`

## Authentication

By default it tries:

1. `INSDL_COOKIE` / `INSDL_COOKIE_STRING`
2. local `.env`
3. `scripts/cookie.txt`
4. local Chromium-family cookie DB + Safe Storage decryption

Optional env hints:

```bash
INSDL_BROWSER=chromium
INSDL_CHROME_PROFILE=Default
```

## Notes

- Only Instagram-domain cookies are sent
- Cookie values are treated as secrets and never printed
- If Termux cannot write `/sdcard`, run:

```bash
termux-setup-storage
```
