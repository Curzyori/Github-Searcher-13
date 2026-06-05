import asyncio
import os
import re
import sys
from pathlib import Path
import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants & Styling
# ---------------------------------------------------------------------------
ENV_PATH = Path(__file__).parent / ".env"
OUTPUT_FILE = Path(__file__).parent / "repo.txt"

class TerminalStyle:
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
    f"║             ⚙️  [GITHUB REPOSITORY SCRAPER]               ║\n"
    f"║                Query: 'antigravity'                      ║\n"
    f"╚══════════════════════════════════════════════════════════╝"
    f"{TS.RESET}\n"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean_html(text: str) -> str:
    """Remove HTML tags like <em> and </em> from a string."""
    return re.sub(r'<[^>]*>', '', text)

def upsert_env(key: str, value: str) -> None:
    """Insert or replace key in .env."""
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
# Core Scraper
# ---------------------------------------------------------------------------
async def scrape_repo_page(client: httpx.AsyncClient, page: int) -> list[str] | None:
    """Scrapes a single page of repository search results."""
    url = "https://github.com/search"
    params = {
        "q": "antigravity",
        "type": "repositories",
        "p": page
    }
    
    try:
        resp = await client.get(url, params=params)
    except httpx.RequestError as exc:
        print(f"{TS.badge_alert()} Network error di page {page}: {TS.TEXT_RED}{exc}{TS.RESET}")
        return None

    if resp.status_code == 429:
        print(f"{TS.badge_alert()} Rate-limited (429 Too Many Requests) di page {page}.")
        return None
    elif resp.status_code in (401, 403):
        print(f"{TS.badge_alert()} Unauthorized/Forbidden ({resp.status_code}) di page {page}. Sesi kadaluarsa?")
        return None
    elif resp.status_code != 200:
        print(f"{TS.badge_alert()} HTTP {TS.TEXT_RED}{resp.status_code}{TS.RESET} di page {page}.")
        return None

    try:
        data = resp.json()
    except Exception:
        print(f"{TS.badge_alert()} Respon bukan JSON valid di page {page}.")
        return None

    payload = data.get("payload", {})
    results = payload.get("results", [])
    
    repos = []
    for item in results:
        hl_name = item.get("hl_name", "")
        if hl_name:
            clean_name = clean_html(hl_name)
            repos.append(f"github.com/{clean_name}")
        else:
            repo_data = item.get("repo", {}).get("repository", {})
            owner = repo_data.get("owner_login")
            name = repo_data.get("name")
            if owner and name:
                repos.append(f"github.com/{owner}/{name}")
                
    return repos

async def main():
    print(BANNER)
    load_dotenv(ENV_PATH)
    
    session_cookie = os.getenv("GITHUB_SESSION")
    
    while True:
        if not session_cookie:
            print(f"{TS.badge_alert()} GITHUB_SESSION cookie tidak ditemukan di .env.")
            session_cookie = input("  🔑 Masukkan user_session cookie Anda: ").strip()
            if not session_cookie:
                print("[!] Cookie kosong. Keluar.")
                sys.exit(1)
            upsert_env("GITHUB_SESSION", session_cookie)
            print(f"[*] Sesi disimpan ke .env.")

        # Test request to make sure session works
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": f"user_session={session_cookie}"
        }
        
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            test_resp = await client.get("https://github.com/search", params={"q": "antigravity", "type": "repositories", "p": 1})
            if test_resp.status_code in (401, 403, 429) or "payload" not in test_resp.text:
                print(f"{TS.badge_alert()} Sesi di .env tidak valid atau kena rate limit (HTTP {test_resp.status_code}).")
                session_cookie = None  # Force re-input
                continue
            else:
                break

    try:
        pages_input = input("  📄 Masukkan jumlah halaman yang ingin di-scrape: ").strip()
        max_pages = int(pages_input) if pages_input.isdigit() else 1
    except KeyboardInterrupt:
        print("\n[*] Dibatalkan.")
        sys.exit(0)

    all_repos = []
    
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": f"user_session={session_cookie}"
    }

    print(f"\n[*] Mulai scraping {max_pages} halaman...")
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        for page in range(1, max_pages + 1):
            print(f"[*] Scraping Page {page}...")
            repos = await scrape_repo_page(client, page)
            if repos is None:
                print(f"[!] Hentikan proses scraping karena error di page {page}.")
                break
            if not repos:
                print(f"[*] Halaman {page} tidak memiliki hasil lagi. Selesai.")
                break
            
            print(f"{TS.badge_done()} Berhasil mengambil {len(repos)} repo.")
            for repo in repos:
                print(f"  🔗 {repo}")
                all_repos.append(repo)
            
            if page < max_pages:
                await asyncio.sleep(2.5)

    if all_repos:
        # Write to repo.txt
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            for repo in all_repos:
                f.write(repo + "\n")
        print(f"\n{TS.badge_success()} Sukses! {len(all_repos)} repositori tersimpan di {TS.TEXT_GREEN}repo.txt{TS.RESET}.")
    else:
        print(f"\n{TS.badge_alert()} Tidak ada repositori yang ditemukan/disimpan.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Dibatalkan oleh user.")
        sys.exit(0)
