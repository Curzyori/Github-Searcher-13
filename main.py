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


# ---------------------------------------------------------------------------
# Terminal ANSI styling
# ---------------------------------------------------------------------------

class TerminalStyle:
    """ANSI escape code manager for terminal background colors."""

    BANNER_BG = "\033[48;5;17m\033[97m"    # Dark blue bg + bright white text
    SUCCESS_BG = "\033[48;5;22m\033[97m"   # Dark green bg + bright white text
    WARNING_BG = "\033[48;5;124m\033[97m"  # Dark red/amber bg + bright white text
    TEXT_GREEN = "\033[38;5;82m"           # Bright green text
    TEXT_RED = "\033[38;5;196m"            # Bright red text
    TEXT_MUTED = "\033[38;5;244m"          # Muted gray text
    RESET = "\033[0m"

    @classmethod
    def banner(cls, text: str) -> str:
        return f"{cls.BANNER_BG}{text}{cls.RESET}"

    @classmethod
    def badge_success(cls) -> str:
        return f"{cls.SUCCESS_BG}[ SUCCESS ]{cls.RESET}"

    @classmethod
    def badge_alert(cls) -> str:
        return f"{cls.WARNING_BG}[ ALERT ]{cls.RESET}"

    @classmethod
    def badge_done(cls) -> str:
        return f"{cls.SUCCESS_BG}[ DONE ]{cls.RESET}"


TS = TerminalStyle

BANNER = (
    f"\n{TS.BANNER_BG}"
    f"╔══════════════════════════════════════════════════════════╗\n"
    f"║                ⚙️  [GITHUB SEARCHER (13)]                 ║\n"
    f"║         Collab Project: @Curzyori x @Seeyaa77            ║\n"
    f"╚══════════════════════════════════════════════════════════╝"
    f"{TS.RESET}\n"
)


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

async def github_login(email: str, password: str) -> str | None:
    """
    Full browser-emulated login to github.com with multi-stage 2FA support.

    1. GET /login, harvest every hidden input from the session form.
    2. POST /session with harvested payload + browser emulation fields.
    3. If GitHub responds with a 302 to the two-factor challenge URL,
       follow that redirect, extract the new authenticity_token, prompt
       the user for an OTP code, and POST the 2FA verification.
    4. Return the user_session cookie value on success.
    """
    ua_header = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
        headers={"User-Agent": ua_header},
    ) as client:

        # ---------------------------------------------------------------
        # Stage 1: GET /login and dynamically harvest form inputs
        # ---------------------------------------------------------------
        try:
            login_page = await client.get(GITHUB_LOGIN_URL)
        except httpx.RequestError as exc:
            print(
                f"{TS.badge_alert()} Gagal konek ke GitHub login page: "
                f"{TS.TEXT_RED}{exc}{TS.RESET}"
            )
            return None

        # Follow any initial redirects manually (GitHub sometimes 302s
        # the bare /login URL before serving the actual page).
        while login_page.status_code in (301, 302):
            redirect_url = login_page.headers.get("Location", "")
            if not redirect_url:
                break
            if redirect_url.startswith("/"):
                redirect_url = f"https://github.com{redirect_url}"
            try:
                login_page = await client.get(redirect_url)
            except httpx.RequestError as exc:
                print(
                    f"{TS.badge_alert()} Redirect gagal: "
                    f"{TS.TEXT_RED}{exc}{TS.RESET}"
                )
                return None

        if login_page.status_code != 200:
            print(
                f"{TS.badge_alert()} Login page returned HTTP "
                f"{TS.TEXT_RED}{login_page.status_code}{TS.RESET}"
            )
            return None

        soup = BeautifulSoup(login_page.text, "html.parser")
        login_form = soup.select_one('form[action="/session"]')
        if login_form is None:
            print(f"{TS.badge_alert()} Tidak bisa menemukan form login di halaman.")
            return None

        # Harvest every <input> inside the form into a baseline payload.
        form_data: dict[str, str] = {}
        for input_tag in login_form.find_all("input"):
            field_name = input_tag.get("name")
            if field_name:
                form_data[field_name] = input_tag.get("value", "")

        # Inject mandatory browser emulation fields on top of the
        # harvested baseline. These overwrite any matching keys.
        form_data.update({
            "login": email,
            "password": password,
            "commit": "Sign in",
            "webauthn-conditional": "undefined",
            "javascript-support": "true",
            "webauthn-support": "supported",
            "webauthn-iuvpaa-support": "supported",
            "return_to": "https://github.com/login",
        })

        # ---------------------------------------------------------------
        # Stage 2: POST /session (follow_redirects=False to inspect 302)
        # ---------------------------------------------------------------
        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": GITHUB_LOGIN_URL,
            "Origin": "https://github.com",
        }

        try:
            session_resp = await client.post(
                GITHUB_SESSION_URL,
                data=form_data,
                headers=post_headers,
            )
        except httpx.RequestError as exc:
            print(
                f"{TS.badge_alert()} Gagal POST ke /session: "
                f"{TS.TEXT_RED}{exc}{TS.RESET}"
            )
            return None

        # ---------------------------------------------------------------
        # Stage 3: Multi-stage 2FA lifecycle
        # ---------------------------------------------------------------
        if session_resp.status_code == 302:
            redirect_location = session_resp.headers.get("Location", "")

            if "two-factor" in redirect_location:
                # Build the full challenge URL if GitHub returned a relative path.
                if redirect_location.startswith("/"):
                    challenge_url = f"https://github.com{redirect_location}"
                else:
                    challenge_url = redirect_location

                print(f"\n[*] GitHub memerlukan verifikasi 2FA ...")
                print(f"[*] Mengambil halaman challenge: {challenge_url}")

                try:
                    twofa_page = await client.get(challenge_url)
                except httpx.RequestError as exc:
                    print(
                        f"{TS.badge_alert()} Gagal GET halaman 2FA: "
                        f"{TS.TEXT_RED}{exc}{TS.RESET}"
                    )
                    return None

                # Follow redirects on the 2FA page too, if any.
                while twofa_page.status_code in (301, 302):
                    next_url = twofa_page.headers.get("Location", "")
                    if not next_url:
                        break
                    if next_url.startswith("/"):
                        next_url = f"https://github.com{next_url}"
                    try:
                        twofa_page = await client.get(next_url)
                    except httpx.RequestError as exc:
                        print(
                            f"{TS.badge_alert()} Redirect 2FA gagal: "
                            f"{TS.TEXT_RED}{exc}{TS.RESET}"
                        )
                        return None

                twofa_soup = BeautifulSoup(twofa_page.text, "html.parser")
                twofa_token_input = twofa_soup.find(
                    "input", attrs={"name": "authenticity_token"}
                )
                if twofa_token_input is None:
                    print(
                        f"{TS.badge_alert()} Tidak bisa menemukan "
                        f"authenticity_token di halaman 2FA."
                    )
                    return None

                twofa_token = twofa_token_input.get("value", "")

                # Prompt the user for the verification code.
                otp_code = input("  [?] Masukkan kode 2FA / OTP: ").strip()
                if not otp_code:
                    print(f"{TS.badge_alert()} Kode 2FA kosong. Membatalkan login.")
                    return None

                twofa_payload = {
                    "authenticity_token": twofa_token,
                    "app_otp": otp_code,
                }

                twofa_headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": challenge_url,
                    "Origin": "https://github.com",
                }

                print("[*] Mengirim verifikasi 2FA ...")

                try:
                    twofa_resp = await client.post(
                        "https://github.com/sessions/two-factor",
                        data=twofa_payload,
                        headers=twofa_headers,
                    )
                except httpx.RequestError as exc:
                    print(
                        f"{TS.badge_alert()} Gagal POST 2FA: "
                        f"{TS.TEXT_RED}{exc}{TS.RESET}"
                    )
                    return None

                # Check for 2FA error in the response body.
                if twofa_resp.status_code == 200:
                    error_soup = BeautifulSoup(twofa_resp.text, "html.parser")
                    flash_alert = error_soup.select_one(".js-flash-alert")
                    if flash_alert:
                        alert_text = flash_alert.get_text(strip=True)
                        print(
                            f"{TS.badge_alert()} Two-factor authentication failed"
                            f" | {alert_text}"
                        )
                        return None

                # After successful 2FA, check for the session cookie.
                for cookie in client.cookies.jar:
                    if cookie.name == "user_session":
                        return cookie.value

                # If the 2FA POST itself returns a 302, that usually
                # means success. Follow and grab the cookie.
                if twofa_resp.status_code == 302:
                    for cookie in client.cookies.jar:
                        if cookie.name == "user_session":
                            return cookie.value

                print(
                    f"{TS.badge_alert()} Two-factor authentication failed. "
                    f"Tidak ada session cookie setelah 2FA."
                )
                return None

        # ---------------------------------------------------------------
        # Stage 4: Non-2FA path, check for session cookie or errors
        # ---------------------------------------------------------------

        # Successful login usually returns a 302 redirect.
        if session_resp.status_code == 302:
            for cookie in client.cookies.jar:
                if cookie.name == "user_session":
                    return cookie.value

        # If the response is 200, GitHub re-rendered the login page
        # with an error message. Inspect for .js-flash-alert.
        if session_resp.status_code == 200:
            error_soup = BeautifulSoup(session_resp.text, "html.parser")
            flash_alert = error_soup.select_one(".js-flash-alert")
            if flash_alert:
                alert_text = flash_alert.get_text(strip=True)
                print(
                    f"{TS.badge_alert()} Incorrect username or password"
                    f" | {alert_text}"
                )
                return None

        # Fallback: cookie might have been set even without a clean 302.
        for cookie in client.cookies.jar:
            if cookie.name == "user_session":
                return cookie.value

        print(f"{TS.badge_alert()} Login gagal. Periksa email/password kamu.")
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
        print(f"{TS.badge_alert()} Network error on page {page}: {TS.TEXT_RED}{exc}{TS.RESET}")
        return None

    if resp.status_code == 401:
        print(f"{TS.badge_alert()} Token tidak valid (HTTP 401). Periksa GITHUB_TOKEN.")
        return None
    if resp.status_code == 403:
        reset_ts = resp.headers.get("X-RateLimit-Reset", "unknown")
        print(f"{TS.badge_alert()} Rate-limited (403). Reset at timestamp {TS.TEXT_RED}{reset_ts}{TS.RESET}. Berhenti.")
        return None
    if resp.status_code == 422:
        print(f"[*] Page {page} melewati batas result window. Selesai.")
        return None
    if resp.status_code != 200:
        print(f"{TS.badge_alert()} HTTP {TS.TEXT_RED}{resp.status_code}{TS.RESET} di page {page}. Berhenti.")
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
        print(f"{TS.badge_alert()} Gagal fetch {raw_url}: {TS.TEXT_RED}{exc}{TS.RESET}")
        return None

    if resp.status_code in (401, 403):
        print(f"{TS.badge_alert()} Rate-limited / unauthorized ({resp.status_code}). Skip file.")
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

    print(f"\n[Engine A] Target keyword dikunci: '{keyword}'")
    print(f"[Engine A] Pattern: {pattern.pattern}")
    print(f"[Engine A] Output : {output_dir.resolve()}\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(1, max_pages + 1):
            items = await api_search_page(client, keyword, page, headers)
            if items is None:
                break
            if not items:
                print(f"[*] Page {page}: Tidak ada hasil lagi. Selesai.")
                break

            print(f"[*] Page {page}: Scanning {len(items)} blob target...")

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

            if page_files > 0:
                print(f"{TS.badge_done()}    -> {TS.TEXT_GREEN}{page_files}{TS.RESET} file berhasil diekstrak | {TS.TEXT_GREEN}{page_tokens}{TS.RESET} token unik diamankan")
            else:
                print(f"{TS.TEXT_MUTED}    -> {page_files} file berhasil diekstrak | {page_tokens} token unik diamankan{TS.RESET}")

            if page < max_pages:
                await asyncio.sleep(2.0)

    print(
        f"\n{TS.badge_success()} Proses Selesai. {TS.TEXT_GREEN}{total_files}{TS.RESET} file dump dibuat, "
        f"total {TS.TEXT_GREEN}{total_tokens}{TS.RESET} token unik."
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
        print(f"{TS.badge_alert()} Network error di page {page}: {TS.TEXT_RED}{exc}{TS.RESET}")
        return None

    if resp.status_code in (401, 403):
        print(f"{TS.badge_alert()} Session expired atau rate-limited ({resp.status_code}). Berhenti.")
        return None
    if resp.status_code != 200:
        print(f"{TS.badge_alert()} HTTP {TS.TEXT_RED}{resp.status_code}{TS.RESET} di page {page}. Berhenti.")
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
        print(f"{TS.badge_alert()} Gagal fetch {raw_url}: {TS.TEXT_RED}{exc}{TS.RESET}")
        return 0

    if resp.status_code in (401, 403):
        print(f"{TS.badge_alert()} Rate-limited / unauthorized ({resp.status_code}). Skip file.")
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

    print(f"\n[Engine B] Target keyword dikunci: '{keyword}'")
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
                print(f"[*] Page {page}: Tidak ada hasil lagi. Selesai.")
                break

            print(f"[*] Page {page}: Scanning {len(items)} blob target...")

            tasks = [
                process_web_item(client, item, pattern, output_dir, global_seen)
                for item in items
            ]
            results = await asyncio.gather(*tasks)

            page_tokens = sum(results)
            page_files = sum(1 for r in results if r > 0)
            total_tokens += page_tokens
            total_files += page_files

            if page_files > 0:
                print(f"{TS.badge_done()}    -> {TS.TEXT_GREEN}{page_files}{TS.RESET} file berhasil diekstrak | {TS.TEXT_GREEN}{page_tokens}{TS.RESET} token unik diamankan")
            else:
                print(f"{TS.TEXT_MUTED}    -> {page_files} file berhasil diekstrak | {page_tokens} token unik diamankan{TS.RESET}")

            if page < max_pages:
                await asyncio.sleep(3.0)

    print(
        f"\n{TS.badge_success()} Proses Selesai. {TS.TEXT_GREEN}{total_files}{TS.RESET} file dump dibuat, "
        f"total {TS.TEXT_GREEN}{total_tokens}{TS.RESET} token unik."
    )


# ---------------------------------------------------------------------------
# Interactive CLI menu
# ---------------------------------------------------------------------------

def prompt_credentials() -> tuple[str, str]:
    """Prompt user for GitHub credentials in the terminal."""
    email = input("  email/username: ").strip()
    password = getpass.getpass("  password: ")
    return email, password


def interactive_menu() -> None:
    """Top-level interactive menu that drives the whole session."""
    print(BANNER)
    print("  1. Auto Pilot  -> Sesi Browser (.env Session)")
    print("  2. Fast Skip   -> Direct Token (GITHUB_TOKEN)")
    print()

    choice = input("  [?] Pilih Mode Eksekusi [1/2]: ").strip()

    if choice == "1":
        handle_auto_mode()
    elif choice == "2":
        handle_skip_mode()
    else:
        print(f"{TS.badge_alert()} Pilihan tidak valid. Keluar.")
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
        print(f"\n{TS.badge_success()} Session Key Ditemukan di .env. Bypass Login State!")
        session_cookie = existing_session
    else:
        print("\n[*] GITHUB_SESSION belum ada. Mulai login ...\n")
        email, password = prompt_credentials()
        print("\n[*] Mengirim login request ke GitHub ...")

        session_cookie = asyncio.run(github_login(email, password))

        if not session_cookie:
            print(f"{TS.badge_alert()} Gagal mendapatkan session cookie. Keluar.")
            sys.exit(1)

        # Persist to .env so subsequent runs skip the login step.
        upsert_env("GITHUB_SESSION", session_cookie)
        print(f"[*] Session cookie disimpan ke .env (GITHUB_SESSION).")

    # Prompt for search query.
    keyword = input("\n  \U0001f4e5 Masukkan Keyword Pencarian (Contoh: api/skills): ").strip()
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
        print(f"{TS.badge_alert()} GITHUB_TOKEN tidak ditemukan di .env. Keluar.")
        sys.exit(1)

    print(f"\n{TS.badge_success()} Session Key Ditemukan di .env. Bypass Login State!")

    keyword = input("\n  \U0001f4e5 Masukkan Keyword Pencarian (Contoh: api/skills): ").strip()
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