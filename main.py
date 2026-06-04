"""
GitHub Code Search & Scraper CLI -- Dual-Engine Architecture

Two execution modes:
  1. Auto  -- Authenticate via GitHub web login, persist the session cookie,
              then scrape the web search UI (github.com/search).
  2. Skip  -- Use a GITHUB_TOKEN from .env to hit api.github.com/search/code.

Usage:
    python3 main.py
"""

import asyncio
import getpass
import os
import re
import sys
from pathlib import Path

import aiofiles
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENV_PATH = Path(__file__).parent / ".env"
RESULTS_ROOT = Path(__file__).parent / "results"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_LOGIN_URL = "https://github.com/login"
GITHUB_SESSION_URL = "https://github.com/session"
PER_PAGE = 30

BANNER = r"""
  ╔══════════════════════════════════════╗
  ║         [Github Searcher]            ║
  ╚══════════════════════════════════════╝
"""


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def build_pattern(keyword: str) -> re.Pattern:
    """Compile a regex that matches `{keyword}-<alphanum 10..50 chars>`."""
    return re.compile(rf"{re.escape(keyword)}-[a-zA-Z0-9]{{10,50}}")


def sanitize_filename(raw: str) -> str:
    """Replace any character that isn't alphanumeric, underscore, hyphen, or dot."""
    return re.sub(r"[^a-zA-Z0-9_\-.]", "_", raw)


def build_output_filename(owner: str, repo: str, filepath: str) -> str:
    """Flatten owner/repo/path into a single sanitized .txt filename."""
    combined = f"{owner}_{repo}_{filepath}"
    return sanitize_filename(combined) + ".txt"


# ---------------------------------------------------------------------------
# .env mutation -- safe, non-destructive
# ---------------------------------------------------------------------------

def read_env_value(key: str) -> str | None:
    """Read a single key from the local .env without relying on os.environ."""
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def upsert_env(key: str, value: str) -> None:
    """Insert or replace *key* in `.env` without touching other variables."""
    lines: list[str] = []
    replaced = False

    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("#") and "=" in stripped:
                k, _, _ = stripped.partition("=")
                if k.strip() == key:
                    lines.append(f"{key}={value}")
                    replaced = True
                    continue
            lines.append(line)

    if not replaced:
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Authentication -- GitHub web login handshake
# ---------------------------------------------------------------------------

async def github_login(email: str, password: str, otp: str | None) -> str | None:
    """
    Simulate a browser login to github.com.

    1. GET /login to grab the authenticity_token.
    2. POST /session with credentials (+ optional OTP header).
    3. Return the user_session cookie value on success.
    """
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    ) as client:
        # -- Step 1: GET the login page for the CSRF token ----------------
        try:
            login_page = await client.get(GITHUB_LOGIN_URL)
        except httpx.RequestError as exc:
            print(f"[!] Gagal konek ke GitHub login page: {exc}")
            return None

        if login_page.status_code != 200:
            print(f"[!] Login page returned HTTP {login_page.status_code}")
            return None

        soup = BeautifulSoup(login_page.text, "html.parser")
        token_input = soup.find("input", attrs={"name": "authenticity_token"})
        if token_input is None:
            print("[!] Tidak bisa menemukan authenticity_token di login page.")
            return None

        authenticity_token = token_input.get("value", "")

        # -- Step 2: POST credentials ------------------------------------
        form_data = {
            "commit": "Sign in",
            "authenticity_token": authenticity_token,
            "login": email,
            "password": password,
            "trusted_device": "",
            "webauthn-support": "supported",
            "webauthn-iuvpaa-support": "unsupported",
            "return_to": "",
            "timestamp": "",
            "timestamp_secret": "",
        }

        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": GITHUB_LOGIN_URL,
        }
        if otp:
            post_headers["X-GitHub-OTP"] = otp

        try:
            session_resp = await client.post(
                GITHUB_SESSION_URL,
                data=form_data,
                headers=post_headers,
            )
        except httpx.RequestError as exc:
            print(f"[!] Gagal POST ke /session: {exc}")
            return None

        # -- Step 3: fish out user_session cookie -------------------------
        for cookie in client.cookies.jar:
            if cookie.name == "user_session":
                return cookie.value

        # If we landed on a 2FA challenge page, report it.
        if "two-factor" in str(session_resp.url):
            print("[!] GitHub minta 2FA tapi OTP tidak valid atau kosong.")
        else:
            print("[!] Login gagal. Periksa email/password kamu.")
        return None


# ---------------------------------------------------------------------------
# Engine A -- Token-based API search (Skip mode)
# ---------------------------------------------------------------------------

async def api_search_page(
    client: httpx.AsyncClient,
    keyword: str,
    page: int,
    headers: dict,
) -> list[dict] | None:
    """Fetch one page from api.github.com/search/code."""
    params = {"q": keyword, "per_page": PER_PAGE, "page": page}

    try:
        resp = await client.get(
            f"{GITHUB_API_BASE}/search/code",
            params=params,
            headers=headers,
        )
    except httpx.RequestError as exc:
        print(f"[!] Network error on page {page}: {exc}")
        return None

    if resp.status_code == 401:
        print("[!] Token tidak valid (HTTP 401). Periksa GITHUB_TOKEN.")
        return None
    if resp.status_code == 403:
        reset_ts = resp.headers.get("X-RateLimit-Reset", "unknown")
        print(f"[!] Rate-limited (403). Reset at timestamp {reset_ts}. Berhenti.")
        return None
    if resp.status_code == 422:
        print(f"[*] Page {page} melewati batas result window. Selesai.")
        return None
    if resp.status_code != 200:
        print(f"[!] HTTP {resp.status_code} di page {page}. Berhenti.")
        return None

    data = resp.json()
    return data.get("items", [])


async def fetch_raw_content(
    client: httpx.AsyncClient,
    raw_url: str,
    headers: dict,
) -> str | None:
    """Download raw text of a single file from GitHub."""
    try:
        resp = await client.get(raw_url, headers=headers, follow_redirects=True)
    except httpx.RequestError as exc:
        print(f"[!] Gagal fetch {raw_url}: {exc}")
        return None

    if resp.status_code in (401, 403):
        print(f"[!] Rate-limited / unauthorized ({resp.status_code}). Skip file.")
        return None
    if resp.status_code != 200:
        return None

    return resp.text


async def process_api_item(
    client: httpx.AsyncClient,
    item: dict,
    pattern: re.Pattern,
    output_dir: Path,
    headers: dict,
    global_seen: set,
) -> int:
    """Process a single API search result item (Engine A)."""
    repo_data = item.get("repository", {})
    owner = repo_data.get("owner", {}).get("login", "unknown")
    repo_name = repo_data.get("name", "unknown")
    filepath = item.get("path", "unknown")
    repo_url = repo_data.get("html_url", f"https://github.com/{owner}/{repo_name}")

    default_branch = repo_data.get("default_branch", "main")
    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{owner}/{repo_name}/{default_branch}/{filepath}"
    )

    content = await fetch_raw_content(client, raw_url, headers)
    if content is None:
        return 0

    matches = set(pattern.findall(content))
    if not matches:
        return 0

    new_tokens = matches - global_seen
    if not new_tokens:
        return 0

    global_seen.update(new_tokens)

    out_name = build_output_filename(owner, repo_name, filepath)
    out_path = output_dir / out_name

    header = (
        f"# Source Repo: {repo_url}\n"
        f"# File Path: {filepath}\n"
        "---\n"
    )

    async with aiofiles.open(out_path, mode="w", encoding="utf-8") as fh:
        await fh.write(header)
        for token in sorted(new_tokens):
            await fh.write(token + "\n")

    return len(new_tokens)


async def run_engine_a(keyword: str, token: str, max_pages: int) -> None:
    """Engine A (Skip): paginate api.github.com/search/code with a token."""
    output_dir = RESULTS_ROOT / keyword
    output_dir.mkdir(parents=True, exist_ok=True)

    pattern = build_pattern(keyword)
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
    }

    global_seen: set[str] = set()
    total_tokens = 0
    total_files = 0

    print(f"\n[Engine A] Searching via API for: '{keyword}'")
    print(f"[Engine A] Pattern: {pattern.pattern}")
    print(f"[Engine A] Output : {output_dir.resolve()}\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(1, max_pages + 1):
            items = await api_search_page(client, keyword, page, headers)
            if items is None:
                break
            if not items:
                print(f"[*] Tidak ada hasil lagi di page {page}. Selesai.")
                break

            print(f"[*] Page {page}: memproses {len(items)} item ...")

            tasks = [
                process_api_item(
                    client, item, pattern, output_dir, headers, global_seen
                )
                for item in items
            ]
            results = await asyncio.gather(*tasks)

            page_tokens = sum(results)
            page_files = sum(1 for r in results if r > 0)
            total_tokens += page_tokens
            total_files += page_files

            print(f"    -> {page_files} file ditulis, {page_tokens} token unik")

            if page < max_pages:
                await asyncio.sleep(2.0)

    print(
        f"\n[Engine A] Selesai. {total_files} file dibuat, "
        f"{total_tokens} token unik total."
    )


# ---------------------------------------------------------------------------
# Engine B -- Session-based web scraping (Auto mode)
# ---------------------------------------------------------------------------

def parse_web_search_items(html: str) -> list[dict]:
    """
    Parse GitHub's web search results HTML and return a list of dicts
    with keys: owner, repo, filepath, repo_url.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []

    # GitHub wraps each code result in a div with data-testid="results-list"
    # or individual result links. We look for links pointing to blob paths.
    for link in soup.find_all("a", href=True):
        href = link["href"]
        # Pattern: /{owner}/{repo}/blob/{branch}/{path}
        m = re.match(r"^/([^/]+)/([^/]+)/blob/[^/]+/(.+)$", href)
        if m:
            owner, repo, filepath = m.group(1), m.group(2), m.group(3)
            results.append({
                "owner": owner,
                "repo": repo,
                "filepath": filepath,
                "repo_url": f"https://github.com/{owner}/{repo}",
            })

    # Deduplicate (same link can appear multiple times).
    seen = set()
    unique: list[dict] = []
    for item in results:
        key = (item["owner"], item["repo"], item["filepath"])
        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


async def web_search_page(
    client: httpx.AsyncClient,
    keyword: str,
    page: int,
) -> list[dict] | None:
    """Fetch one page from github.com/search?type=code."""
    params = {"q": keyword, "type": "code", "p": page}

    try:
        resp = await client.get(
            "https://github.com/search",
            params=params,
        )
    except httpx.RequestError as exc:
        print(f"[!] Network error di page {page}: {exc}")
        return None

    if resp.status_code in (401, 403):
        print(f"[!] Session expired atau rate-limited ({resp.status_code}). Berhenti.")
        return None
    if resp.status_code != 200:
        print(f"[!] HTTP {resp.status_code} di page {page}. Berhenti.")
        return None

    return parse_web_search_items(resp.text)


async def process_web_item(
    client: httpx.AsyncClient,
    item: dict,
    pattern: re.Pattern,
    output_dir: Path,
    global_seen: set,
) -> int:
    """Process a single web search result item (Engine B)."""
    owner = item["owner"]
    repo = item["repo"]
    filepath = item["filepath"]
    repo_url = item["repo_url"]

    raw_url = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{filepath}"
    )

    try:
        resp = await client.get(raw_url, follow_redirects=True)
    except httpx.RequestError as exc:
        print(f"[!] Gagal fetch {raw_url}: {exc}")
        return 0

    if resp.status_code in (401, 403):
        print(f"[!] Rate-limited / unauthorized ({resp.status_code}). Skip file.")
        return 0
    if resp.status_code != 200:
        return 0

    content = resp.text

    matches = set(pattern.findall(content))
    if not matches:
        return 0

    new_tokens = matches - global_seen
    if not new_tokens:
        return 0

    global_seen.update(new_tokens)

    out_name = build_output_filename(owner, repo, filepath)
    out_path = output_dir / out_name

    header = (
        f"# Source Repo: {repo_url}\n"
        f"# File Path: {filepath}\n"
        "---\n"
    )

    async with aiofiles.open(out_path, mode="w", encoding="utf-8") as fh:
        await fh.write(header)
        for token in sorted(new_tokens):
            await fh.write(token + "\n")

    return len(new_tokens)


async def run_engine_b(keyword: str, session_cookie: str, max_pages: int) -> None:
    """Engine B (Auto): scrape github.com/search with a session cookie."""
    output_dir = RESULTS_ROOT / keyword
    output_dir.mkdir(parents=True, exist_ok=True)

    pattern = build_pattern(keyword)

    global_seen: set[str] = set()
    total_tokens = 0
    total_files = 0

    print(f"\n[Engine B] Searching via web UI for: '{keyword}'")
    print(f"[Engine B] Pattern: {pattern.pattern}")
    print(f"[Engine B] Output : {output_dir.resolve()}\n")

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Cookie": f"user_session={session_cookie}",
        },
        follow_redirects=True,
    ) as client:
        for page in range(1, max_pages + 1):
            items = await web_search_page(client, keyword, page)
            if items is None:
                break
            if not items:
                print(f"[*] Tidak ada hasil lagi di page {page}. Selesai.")
                break

            print(f"[*] Page {page}: memproses {len(items)} item ...")

            tasks = [
                process_web_item(client, item, pattern, output_dir, global_seen)
                for item in items
            ]
            results = await asyncio.gather(*tasks)

            page_tokens = sum(results)
            page_files = sum(1 for r in results if r > 0)
            total_tokens += page_tokens
            total_files += page_files

            print(f"    -> {page_files} file ditulis, {page_tokens} token unik")

            if page < max_pages:
                await asyncio.sleep(3.0)

    print(
        f"\n[Engine B] Selesai. {total_files} file dibuat, "
        f"{total_tokens} token unik total."
    )


# ---------------------------------------------------------------------------
# Interactive CLI menu
# ---------------------------------------------------------------------------

def prompt_credentials() -> tuple[str, str, str | None]:
    """Prompt user for GitHub credentials in the terminal."""
    email = input("  email/username: ").strip()
    password = getpass.getpass("  password: ")
    otp = input("  2fa (jika user menggunakan 2fa): ").strip() or None
    return email, password, otp


def interactive_menu() -> None:
    """Top-level interactive menu that drives the whole session."""
    print(BANNER)
    print("  1. Auto (With email/password/2fa simulation)")
    print("  2. Skip (IF SUDAH MASUKIN GITHUB_TOKEN di env)")
    print()

    choice = input("  Pilih mode [1/2]: ").strip()

    if choice == "1":
        handle_auto_mode()
    elif choice == "2":
        handle_skip_mode()
    else:
        print("[!] Pilihan tidak valid. Keluar.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------

def handle_auto_mode() -> None:
    """Auto mode: authenticate via web login or reuse an existing session."""
    load_dotenv(ENV_PATH)

    # Check if we already have a persisted session cookie.
    existing_session = read_env_value("GITHUB_SESSION")

    if existing_session:
        print(f"\n[*] GITHUB_SESSION ditemukan di .env. Skip login.")
        session_cookie = existing_session
    else:
        print("\n[*] GITHUB_SESSION belum ada. Mulai login ...\n")
        email, password, otp = prompt_credentials()
        print("\n[*] Mengirim login request ke GitHub ...")

        session_cookie = asyncio.run(github_login(email, password, otp))

        if not session_cookie:
            print("[!] Gagal mendapatkan session cookie. Keluar.")
            sys.exit(1)

        # Persist to .env so subsequent runs skip the login step.
        upsert_env("GITHUB_SESSION", session_cookie)
        print(f"[*] Session cookie disimpan ke .env (GITHUB_SESSION).")

    # Prompt for search query.
    keyword = input("\n  input: masukkan input contoh api: ").strip()
    if not keyword:
        print("[!] Keyword kosong. Keluar.")
        sys.exit(1)

    max_pages = 5
    asyncio.run(run_engine_b(keyword, session_cookie, max_pages))


def handle_skip_mode() -> None:
    """Skip mode: use GITHUB_TOKEN from .env to query the API."""
    load_dotenv(ENV_PATH)

    token = os.getenv("GITHUB_TOKEN") or read_env_value("GITHUB_TOKEN")
    if not token:
        print("[!] GITHUB_TOKEN tidak ditemukan di .env. Keluar.")
        sys.exit(1)

    print(f"\n[*] GITHUB_TOKEN ditemukan. Menggunakan Engine A (API).")

    keyword = input("\n  input: masukkan input contoh api: ").strip()
    if not keyword:
        print("[!] Keyword kosong. Keluar.")
        sys.exit(1)

    max_pages = 5
    asyncio.run(run_engine_a(keyword, token, max_pages))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        interactive_menu()
    except KeyboardInterrupt:
        print("\n[*] Dibatalkan oleh user.")
        sys.exit(0)