#!/data/data/com.termux/files/usr/bin/python
# -*- coding: utf-8 -*-

"""Download media from a PUBLIC Instagram post/reel URL and save locally.

Auth source priority:
1. INSDL_COOKIE / INSDL_COOKIE_STRING env vars
2. skill-local .env or workspace .env
3. scripts/cookie.txt
4. Local Chromium cookie DB + Safe Storage decryption

Cookie extraction rules:
- Prefer local browser cookies instead of manual copy/paste
- Use Chromium-family local cookie DB + Safe Storage only
- Allow browser/profile override via env vars
- Keep cookie values secret; only emit the source, never the raw value
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DOC_ID_SHORTCODE = "8845758582119845"  # local workspace version
SHORTCODE_RE = re.compile(r"/(?:p|reel|tv)/([^/]+)/")
INSTAGRAM_DOMAINS = {"instagram.com", ".instagram.com", "www.instagram.com"}
DEFAULT_BROWSER_ORDER = ["chrome", "chromium", "edge", "firefox", "brave", "arc"]
CHROMIUM_BROWSERS = {"chrome", "chromium", "edge", "brave", "arc"}
SAFE_STORAGE_DISPLAY_NAMES = {
    "chrome": ["Chrome Safe Storage", "Google Chrome Safe Storage"],
    "chromium": ["Chromium Safe Storage"],
    "edge": ["Microsoft Edge Safe Storage"],
    "brave": ["Brave Safe Storage", "Brave Browser Safe Storage"],
    "arc": ["Arc Safe Storage"],
}

# Retry config
MAX_RETRIES = 1  # one retry max
BASE_RETRY_DELAY = 30  # seconds


class InsdlError(Exception):
    code = "UNKNOWN"


class AuthRequiredError(InsdlError):
    code = "AUTH_REQUIRED"


class RateLimitedError(InsdlError):
    code = "RATE_LIMITED"


class PrivateOrUnavailableError(InsdlError):
    code = "PRIVATE_OR_UNAVAILABLE"


class NetworkError(InsdlError):
    code = "NETWORK"


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def parse_shortcode(url: str) -> str:
    m = SHORTCODE_RE.search(url)
    if not m:
        raise ValueError("URL must contain /p/<shortcode>/, /reel/<shortcode>/, or /tv/<shortcode>/")
    return m.group(1)


def build_graphql_url(shortcode: str) -> str:
    variables = {
        "shortcode": shortcode,
        "fetch_tagged_user_count": None,
        "hoisted_comment_id": None,
        "hoisted_reply_id": None,
    }
    variables_s = json.dumps(variables, separators=(",", ":"))
    return (
        "https://www.instagram.com/graphql/query/?doc_id="
        + DOC_ID_SHORTCODE
        + "&variables="
        + urllib.parse.quote(variables_s, safe="")
    )


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (len(v) >= 2) and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def _cookie_value_from_string(cookie_string: str, name: str) -> str:
    prefix = name + "="
    for part in cookie_string.split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return item[len(prefix):]
    return ""


def _is_instagram_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    return domain in INSTAGRAM_DOMAINS or domain.endswith(".instagram.com")


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _is_instagram_host(url: str) -> bool:
    return _is_instagram_domain(_host(url))


def _request_headers(*, cookie: str | None, referer: str | None, target_url: str) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
    if cookie and _is_instagram_host(target_url):
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    return headers


def _env_cookie_value(env_map: dict[str, str]) -> str | None:
    for key in ("INSDL_COOKIE", "INSDL_COOKIE_STRING"):
        value = (env_map.get(key) or "").strip()
        if value:
            return value
    return None


def _browser_root(browser_name: str) -> str | None:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        mapping = {
            "chrome": os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            "chromium": os.path.join(home, "Library", "Application Support", "Chromium"),
            "edge": os.path.join(home, "Library", "Application Support", "Microsoft Edge"),
            "brave": os.path.join(home, "Library", "Application Support", "BraveSoftware", "Brave-Browser"),
            "arc": os.path.join(home, "Library", "Application Support", "Arc", "User Data"),
        }
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        mapping = {
            "chrome": os.path.join(local, "Google", "Chrome", "User Data"),
            "chromium": os.path.join(local, "Chromium", "User Data"),
            "edge": os.path.join(local, "Microsoft", "Edge", "User Data"),
            "brave": os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
            "arc": os.path.join(local, "Arc", "User Data"),
        }
    else:
        mapping = {
            "chrome": os.path.join(home, ".config", "google-chrome"),
            "chromium": os.path.join(home, ".config", "chromium"),
            "edge": os.path.join(home, ".config", "microsoft-edge"),
            "brave": os.path.join(home, ".config", "BraveSoftware", "Brave-Browser"),
            "arc": os.path.join(home, ".config", "Arc", "User Data"),
        }
    return mapping.get(browser_name)


def _iter_chromium_cookie_files(browser_name: str) -> list[str]:
    root = _browser_root(browser_name)
    if not root or not os.path.isdir(root):
        return []

    def existing_cookie_paths(profile_dir: str) -> list[str]:
        out: list[str] = []
        for candidate in (
            os.path.join(profile_dir, "Cookies"),
            os.path.join(profile_dir, "Network", "Cookies"),
        ):
            if os.path.exists(candidate):
                out.append(candidate)
        return out

    env_profile = os.environ.get("INSDL_CHROME_PROFILE", "").strip()
    if env_profile:
        return existing_cookie_paths(os.path.join(root, env_profile))

    paths: list[str] = []
    seen: set[str] = set()
    profile_dirs = [os.path.join(root, "Default")]
    profile_dirs.extend(sorted(glob.glob(os.path.join(root, "Profile *"))))
    for profile_dir in profile_dirs:
        for candidate in existing_cookie_paths(profile_dir):
            if candidate not in seen:
                seen.add(candidate)
                paths.append(candidate)
    return paths


def _get_browser_order() -> list[str]:
    env_browser = os.environ.get("INSDL_BROWSER", "").strip().lower()
    if not env_browser:
        return list(DEFAULT_BROWSER_ORDER)
    if env_browser not in DEFAULT_BROWSER_ORDER:
        return list(DEFAULT_BROWSER_ORDER)
    return [env_browser] + [name for name in DEFAULT_BROWSER_ORDER if name != env_browser]


def _profile_name_from_cookie_file(cookie_file: str) -> str:
    path = Path(cookie_file)
    if path.parent.name == "Network":
        return path.parent.parent.name or "Default"
    return path.parent.name or "Default"


def _load_safe_storage_secret(browser_name: str) -> tuple[str | None, list[str]]:
    diagnostics: list[str] = []
    env_secret = (os.environ.get("INSDL_SAFE_STORAGE_SECRET") or "").strip()
    if env_secret:
        return env_secret, diagnostics

    display_names = SAFE_STORAGE_DISPLAY_NAMES.get(browser_name, [])
    keyring_dir = Path.home() / ".local" / "share" / "keyrings"
    if not keyring_dir.exists():
        diagnostics.append(f"{browser_name}: keyring dir not found")
        return None, diagnostics

    keyring_files = sorted(keyring_dir.glob("*.keyring"))
    if not keyring_files:
        diagnostics.append(f"{browser_name}: no .keyring files found")
        return None, diagnostics

    for keyring_file in keyring_files:
        try:
            text = keyring_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            diagnostics.append(f"{browser_name}: failed reading {keyring_file.name}: {exc}")
            continue

        for display_name in display_names:
            marker = f"display-name={display_name}"
            idx = text.find(marker)
            if idx < 0:
                continue
            secret_idx = text.find("\nsecret=", idx)
            if secret_idx < 0:
                diagnostics.append(f"{browser_name}: found {display_name} but no secret field")
                continue
            line_end = text.find("\n", secret_idx + 1)
            if line_end < 0:
                line_end = len(text)
            secret = text[secret_idx + len("\nsecret=") : line_end].strip()
            if secret:
                diagnostics.append(f"{browser_name}: safe storage secret found in {keyring_file.name}")
                return secret, diagnostics

    diagnostics.append(f"{browser_name}: safe storage secret not found")
    return None, diagnostics


def _decrypt_chromium_cookie_value(encrypted_value: bytes, host_key: str, safe_storage_secret: str) -> str | None:
    if not encrypted_value:
        return None
    if not encrypted_value.startswith(b"v10") and not encrypted_value.startswith(b"v11"):
        return None

    js = r'''
const crypto = require('crypto');
const encryptedHex = process.argv[1];
const safeStorageSecret = process.argv[2];
const hostKey = process.argv[3];
const encrypted = Buffer.from(encryptedHex, 'hex');
const cipherText = encrypted.subarray(3);
const key = crypto.pbkdf2Sync(Buffer.from(safeStorageSecret, 'utf8'), 'saltysalt', 1, 16, 'sha1');
const iv = Buffer.alloc(16, 0x20);
const expectedHostHash = crypto.createHash('sha256').update(hostKey, 'utf8').digest();
try {
  const decipher = crypto.createDecipheriv('aes-128-cbc', key, iv);
  decipher.setAutoPadding(false);
  let out = Buffer.concat([decipher.update(cipherText), decipher.final()]);
  const pad = out[out.length - 1];
  if (pad < 1 || pad > 16 || pad > out.length) {
    process.exit(2);
  }
  out = out.subarray(0, out.length - pad);
  if (out.length < 32) {
    process.exit(3);
  }
  const hostHash = out.subarray(0, 32);
  if (!hostHash.equals(expectedHostHash)) {
    process.exit(4);
  }
  process.stdout.write(out.subarray(32).toString('utf8'));
} catch (err) {
  process.exit(5);
}
'''
    try:
        result = subprocess.run(
            ["node", "-e", js, encrypted_value.hex(), safe_storage_secret, host_key],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    return result.stdout


def _extract_cookie_from_local_chromium() -> tuple[str | None, str | None, list[str]]:
    diagnostics: list[str] = []

    for browser_name in _get_browser_order():
        if browser_name not in CHROMIUM_BROWSERS:
            continue

        cookie_files = _iter_chromium_cookie_files(browser_name)
        if not cookie_files:
            diagnostics.append(f"{browser_name}: no cookie DB found")
            continue

        safe_storage_secret, secret_diag = _load_safe_storage_secret(browser_name)
        diagnostics.extend(secret_diag)
        if not safe_storage_secret:
            continue

        for cookie_file in cookie_files:
            profile_name = _profile_name_from_cookie_file(cookie_file)
            try:
                con = sqlite3.connect(f"file:{cookie_file}?mode=ro", uri=True)
                cur = con.cursor()
                rows = cur.execute(
                    "select host_key, name, value, encrypted_value from cookies where host_key like ? order by host_key, name",
                    ("%instagram.com",),
                ).fetchall()
                version_row = cur.execute("select value from meta where key='version'").fetchone()
                con.close()
            except Exception as exc:
                diagnostics.append(f"{browser_name}[{profile_name}]: sqlite read failed: {exc}")
                continue

            if not rows:
                diagnostics.append(f"{browser_name}[{profile_name}]: no instagram cookies")
                continue

            db_version = int((version_row or ["0"])[0] or "0")
            if db_version < 24:
                diagnostics.append(f"{browser_name}[{profile_name}]: unsupported cookie DB version {db_version}")
                continue

            cookies: dict[str, str] = {}
            failures = 0

            for host_key, name, value, encrypted_value in rows:
                if value:
                    cookies[name] = value
                    continue
                decrypted = _decrypt_chromium_cookie_value(encrypted_value, host_key, safe_storage_secret)
                if decrypted is None:
                    failures += 1
                    continue
                cookies[name] = decrypted

            if "sessionid" in cookies:
                cookie_string = "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))
                diagnostics.append(
                    f"{browser_name}[{profile_name}]: extracted {len(cookies)} instagram cookies via local chromium DB"
                )
                return cookie_string, f"browser:{browser_name}[{profile_name}]", diagnostics

            diagnostics.append(
                f"{browser_name}[{profile_name}]: found {len(rows)} instagram cookie rows but failed to recover sessionid"
            )
            if failures:
                diagnostics.append(f"{browser_name}[{profile_name}]: decrypt failures={failures}")

    return None, None, diagnostics


def load_cookie(script_path: Path) -> tuple[str | None, str | None, list[str]]:
    diagnostics: list[str] = []

    env_cookie = _env_cookie_value(os.environ)
    if env_cookie:
        return env_cookie, "env", diagnostics

    skill_root = script_path.parent.parent
    env_paths = [skill_root / ".env", Path("/data/data/com.termux/files/home/.openclaw/workspace/.env")]
    for env_path in env_paths:
        vals = _parse_dotenv(env_path)
        cookie = _env_cookie_value(vals)
        if cookie:
            return cookie, f"dotenv:{env_path}", diagnostics

    cookie_file = script_path.parent / "cookie.txt"
    if cookie_file.exists():
        cookie = cookie_file.read_text(encoding="utf-8", errors="replace").strip()
        if cookie:
            return cookie, f"file:{cookie_file}", diagnostics

    cookie, source, local_diag = _extract_cookie_from_local_chromium()
    diagnostics.extend(local_diag)
    if cookie:
        return cookie, source, diagnostics

    return None, None, diagnostics


def fetch_json_with_retry(
    url: str,
    *,
    timeout: int = 30,
    max_retries: int = MAX_RETRIES,
    cookie: str | None = None,
    referer: str | None = None,
) -> dict:
    attempt = 0
    last_err: Exception | None = None

    while attempt <= max_retries:
        try:
            req = urllib.request.Request(url, headers=_request_headers(cookie=cookie, referer=referer, target_url=url))
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
            obj = json.loads(raw)

            status = str(obj.get("status", "ok"))
            require_login = bool(obj.get("require_login", False))
            message = str(obj.get("message", ""))
            msg_l = message.lower()

            if require_login:
                raise AuthRequiredError(message or "require_login=true")
            if "wait a few minutes" in msg_l or "rate" in msg_l:
                raise RateLimitedError(message or "rate limited")
            if status == "fail":
                raise PrivateOrUnavailableError(message or "status=fail")

            return obj

        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (401, 403):
                raise AuthRequiredError(f"HTTP {e.code}")
            if e.code == 429 or 500 <= e.code < 600:
                err = RateLimitedError(f"HTTP {e.code}")
            elif e.code in (404, 410):
                raise PrivateOrUnavailableError(f"HTTP {e.code}")
            else:
                raise InsdlError(f"HTTP {e.code}")
        except urllib.error.URLError as e:
            last_err = e
            err = NetworkError(str(e))
        except RateLimitedError as e:
            last_err = e
            err = e
        except (AuthRequiredError, PrivateOrUnavailableError):
            raise
        except json.JSONDecodeError as e:
            raise InsdlError(f"Invalid JSON: {e}")
        except Exception as e:
            last_err = e
            err = NetworkError(str(e))

        if isinstance(err, (RateLimitedError, NetworkError)) and attempt < max_retries:
            eprint(f"ERROR|{err.code}: {err}, retry {attempt}/{max_retries}")
            attempt += 1
            delay = BASE_RETRY_DELAY * attempt
            eprint(f"WAIT|{delay}s before retry...")
            time.sleep(delay)
            continue

        raise err

    raise InsdlError(f"Failed after {max_retries} retries: {last_err}")


def extract_media_urls(obj: dict) -> list[tuple[str, str, str]]:
    media = obj.get("data", {}).get("xdt_shortcode_media")
    if not media:
        raise PrivateOrUnavailableError("No media in response")

    items: list[tuple[str, str, str]] = []  # (url, ext, dedupe_key)

    def add_node(node: dict) -> None:
        media_id = str(node.get("id") or "")
        is_video = bool(node.get("is_video"))
        if is_video and node.get("video_url"):
            u = str(node["video_url"])
            items.append((u, "mp4", media_id or u))
        elif node.get("display_url"):
            u = str(node["display_url"])
            items.append((u, "jpg", media_id or u))

    children = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
    if children:
        for edge in children:
            node = (edge or {}).get("node") or {}
            add_node(node)
    else:
        add_node(media)

    if not items:
        raise PrivateOrUnavailableError("No downloadable media URLs extracted")

    seen: set[str] = set()
    uniq: list[tuple[str, str, str]] = []
    for u, ext, key in items:
        if key in seen:
            continue
        seen.add(key)
        uniq.append((u, ext, key))
    return uniq


def download_file(
    url: str,
    out_path: Path,
    *,
    timeout: int = 60,
    cookie: str | None = None,
    referer: str | None = None,
) -> None:
    req = urllib.request.Request(url, headers=_request_headers(cookie=cookie, referer=referer, target_url=url))
    data = urllib.request.urlopen(req, timeout=timeout).read()

    tmp_path = out_path.with_name(out_path.name + ".part")
    tmp_path.write_bytes(data)
    tmp_path.replace(out_path)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    ts = time.strftime("%Y%m%d_%H%M%S")
    return path.with_name(f"{stem}_{ts}{suffix}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Instagram post/reel URL")
    ap.add_argument("--outdir", default="/sdcard/Pictures/Instagram", help="Base output dir")
    ap.add_argument("--limit", type=int, default=50, help="Max items to download")
    ap.add_argument("--save-json", action="store_true", help="Also save response JSON for debugging")
    ap.add_argument(
        "--auth-check",
        action="store_true",
        help="Check whether a usable Instagram login cookie is available, then exit",
    )
    args = ap.parse_args()

    cookie, auth_source, auth_diag = load_cookie(Path(__file__))

    if args.auth_check:
        if cookie:
            print(f"AUTH|{auth_source}")
            return 0
        eprint("ERROR|AUTH_REQUIRED: no usable Instagram login cookie found")
        for line in auth_diag[:8]:
            eprint(f"DEBUG|auth={line}")
        eprint(
            "HINT|Set INSDL_COOKIE / INSDL_COOKIE_STRING, or login to instagram.com in a supported Chromium-family browser and retry."
        )
        eprint("HINT|Supported browsers: Chrome / Chromium / Edge / Brave / Arc")
        return 4

    if not args.url:
        ap.error("--url is required unless --auth-check is used")

    shortcode = parse_shortcode(args.url)
    target_dir = Path(args.outdir) / shortcode
    target_dir.mkdir(parents=True, exist_ok=True)

    graphql_url = build_graphql_url(shortcode)
    referer = f"https://www.instagram.com/p/{shortcode}/"

    try:
        eprint(f"AUTH|{auth_source or 'none'}")
        eprint(f"FETCH|{shortcode} ...")
        obj = fetch_json_with_retry(graphql_url, cookie=cookie, referer=referer)
        items = extract_media_urls(obj)[: max(1, args.limit)]

        if args.save_json:
            (target_dir / f"{shortcode}.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

        saved = 0
        for idx, (u, ext, _key) in enumerate(items, start=1):
            fp = unique_path(target_dir / f"{shortcode}_{idx:02d}.{ext}")
            download_file(u, fp, cookie=cookie, referer=referer)
            saved += 1

        print(f"SAVED_DIR|{target_dir}")
        print(f"COUNT|{saved}")
        return 0

    except PermissionError as e:
        eprint(f"ERROR|PERMISSION: {e}")
        eprint("HINT|If Termux cannot write /sdcard, run: termux-setup-storage")
        return 3
    except InsdlError as e:
        eprint(f"ERROR|{e.code}: {e}")
        eprint("DEBUG|shortcode=", shortcode)
        eprint("DEBUG|graphql=", graphql_url.replace(DOC_ID_SHORTCODE, "REDACTED"))
        for line in auth_diag[:8]:
            eprint(f"DEBUG|auth={line}")
        if isinstance(e, AuthRequiredError):
            eprint("HINT|Prefer local Chromium login-state decryption over manual cookie copy/paste.")
            eprint(
                "HINT|Set INSDL_COOKIE, or login to instagram.com in a supported Chromium-family browser and retry."
            )
        elif isinstance(e, RateLimitedError):
            eprint("HINT|Rate limited. Wait a bit and retry.")
        elif isinstance(e, PrivateOrUnavailableError):
            eprint("HINT|Post may be private/unavailable for current session.")
        else:
            eprint("HINT|Network/unknown error. Retry later.")
        return 2
    except Exception as e:
        eprint(f"ERROR|UNKNOWN: {e}")
        eprint("DEBUG|shortcode=", shortcode)
        eprint("DEBUG|graphql=", graphql_url.replace(DOC_ID_SHORTCODE, "REDACTED"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
