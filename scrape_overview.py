import asyncio
import os
import sys
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Constants & Styling
# ---------------------------------------------------------------------------
ENV_PATH = Path(__file__).parent / ".env"
REPO_LIST_FILE = Path(__file__).parent / "repo.txt"
OUTPUT_DIR = Path(__file__).parent / "results-repo"

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
    f"║          ⚙️  [GITHUB REPOSITORY OVERVIEW SCRAPER]        ║\n"
    f"║             Processing lists from repo.txt               ║\n"
    f"╚══════════════════════════════════════════════════════════╝"
    f"{TS.RESET}\n"
)

async def scrape_repo_overview(client: httpx.AsyncClient, owner: str, repo: str) -> dict | None:
    """Fetches the overview-files JSON for a repo, falling back from main to master."""
    # Try main branch first
    for branch in ("main", "master"):
        url = f"https://github.com/{owner}/{repo}/overview-files/{branch}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    print(f"{TS.badge_alert()} Respon di branch {branch} bukan JSON valid untuk {owner}/{repo}.")
                    return None
            elif resp.status_code == 429:
                print(f"{TS.badge_alert()} Rate-limited (429) di {owner}/{repo} branch {branch}. Menunggu 15 detik...")
                await asyncio.sleep(15.0)
                # Retry once
                resp = await client.get(url)
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except json.JSONDecodeError:
                        return None
            elif resp.status_code == 404:
                # If main 404s, loop continues to try master
                continue
            else:
                print(f"{TS.badge_alert()} HTTP {resp.status_code} di {owner}/{repo} branch {branch}.")
        except httpx.RequestError as exc:
            print(f"{TS.badge_alert()} Network error di {owner}/{repo} branch {branch}: {exc}")
    
    return None

async def main():
    print(BANNER)
    load_dotenv(ENV_PATH)
    
    session_cookie = os.getenv("GITHUB_SESSION")
    if not session_cookie:
        print(f"{TS.badge_alert()} GITHUB_SESSION cookie tidak ditemukan di .env.")
        sys.exit(1)

    if not REPO_LIST_FILE.exists():
        print(f"{TS.badge_alert()} File repo.txt tidak ditemukan.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Read and parse repositories
    repos = []
    for line in REPO_LIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        # Standardize line. It might contain github.com/owner/repo or just owner/repo
        if "github.com/" in line:
            parts = line.split("github.com/")[-1].split("/")
        else:
            parts = line.split("/")
        
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            repos.append((owner, repo))

    if not repos:
        print(f"{TS.badge_alert()} Tidak ada repository yang valid di repo.txt.")
        sys.exit(0)

    print(f"[*] Ditemukan {len(repos)} repository untuk di-scrape.")
    
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Cookie": f"user_session={session_cookie}"
    }

    # Process sequentially with a delay to respect rate limit
    async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True) as client:
        success_count = 0
        skipped_count = 0
        failed_count = 0

        for i, (owner, repo) in enumerate(repos, 1):
            out_filename = f"{owner}_{repo}.json"
            out_path = OUTPUT_DIR / out_filename

            # Skip if already exists (supports resuming)
            if out_path.exists():
                print(f"{TS.TEXT_MUTED}[{i}/{len(repos)}] Skip {owner}/{repo} (sudah ada di {out_filename}){TS.RESET}")
                skipped_count += 1
                continue

            print(f"[*] [{i}/{len(repos)}] Scraping overview-files {owner}/{repo} ...")
            data = await scrape_repo_overview(client, owner, repo)

            if data:
                try:
                    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                    print(f"{TS.badge_done()} Berhasil menyimpan {TS.TEXT_GREEN}{out_filename}{TS.RESET}")
                    success_count += 1
                except Exception as e:
                    print(f"{TS.badge_alert()} Gagal menulis file {out_filename}: {e}")
                    failed_count += 1
            else:
                print(f"{TS.badge_alert()} Gagal mengambil data untuk {owner}/{repo}")
                failed_count += 1

            # Sleep 1.5 seconds to avoid GitHub rate limits
            await asyncio.sleep(1.5)

    print(f"\n{TS.badge_success()} Selesai!")
    print(f"  ✅ Sukses   : {TS.TEXT_GREEN}{success_count}{TS.RESET}")
    print(f"  ⏭️  Skipped  : {TS.TEXT_MUTED}{skipped_count}{TS.RESET}")
    print(f"  ❌ Gagal    : {TS.TEXT_RED}{failed_count}{TS.RESET}")

if __name__ == "__main__":
    try:
        # Avoid "Event loop is closed" error on Windows
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Dibatalkan oleh user.")
        sys.exit(0)
