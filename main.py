"""
GitHub Code Search & Scraper CLI

Searches GitHub for code matching a keyword pattern, extracts
regex-matched tokens from file contents, deduplicates them,
and writes results to disk with metadata headers.

Usage:
    python main.py <keyword> [--token TOKEN] [--max-pages N]
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import aiofiles
import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_BASE = "https://api.github.com"
RESULTS_ROOT = Path(__file__).parent / "results"
PER_PAGE = 30  # GitHub's default (and max for code search)


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


async def fetch_search_page(
    client: httpx.AsyncClient,
    keyword: str,
    page: int,
    headers: dict,
) -> list[dict] | None:
    """
    Hit the GitHub code-search endpoint for one page of results.

    Returns the list of item dicts, or None when the API says stop.
    """
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

    if resp.status_code == 403:
        reset_ts = resp.headers.get("X-RateLimit-Reset", "unknown")
        print(f"[!] Rate-limited (403). Resets at timestamp {reset_ts}. Stopping.")
        return None

    if resp.status_code == 422:
        # GitHub returns 422 when you ask for a page beyond the result window.
        print(f"[*] Page {page} past result window. Done searching.")
        return None

    if resp.status_code != 200:
        print(f"[!] Unexpected HTTP {resp.status_code} on page {page}. Stopping.")
        return None

    data = resp.json()
    return data.get("items", [])


async def fetch_raw_content(
    client: httpx.AsyncClient,
    raw_url: str,
    headers: dict,
) -> str | None:
    """Download the raw text of a single file from GitHub."""
    try:
        resp = await client.get(raw_url, headers=headers, follow_redirects=True)
    except httpx.RequestError as exc:
        print(f"[!] Failed to fetch {raw_url}: {exc}")
        return None

    if resp.status_code == 403:
        print("[!] Rate-limited while fetching raw content. Skipping file.")
        return None

    if resp.status_code != 200:
        return None

    return resp.text


async def process_item(
    client: httpx.AsyncClient,
    item: dict,
    pattern: re.Pattern,
    output_dir: Path,
    headers: dict,
    global_seen: set,
) -> int:
    """
    Download one search-result file, run regex extraction, deduplicate,
    and write results to disk.

    Returns the count of new unique tokens written.
    """
    repo_data = item.get("repository", {})
    owner = repo_data.get("owner", {}).get("login", "unknown")
    repo_name = repo_data.get("name", "unknown")
    filepath = item.get("path", "unknown")
    repo_url = repo_data.get("html_url", f"https://github.com/{owner}/{repo_name}")

    # Build the raw download URL.
    default_branch = repo_data.get("default_branch", "main")
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{default_branch}/{filepath}"

    content = await fetch_raw_content(client, raw_url, headers)
    if content is None:
        return 0

    # Regex extraction + per-file dedup.
    matches = set(pattern.findall(content))
    if not matches:
        return 0

    # Filter out tokens we already wrote in a previous file.
    new_tokens = matches - global_seen
    if not new_tokens:
        return 0

    global_seen.update(new_tokens)

    # Write the result file.
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


async def run(keyword: str, token: str | None, max_pages: int) -> None:
    """Main async loop: paginate search results and process each file."""
    output_dir = RESULTS_ROOT / keyword
    output_dir.mkdir(parents=True, exist_ok=True)

    pattern = build_pattern(keyword)
    headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    global_seen: set[str] = set()
    total_tokens = 0
    total_files = 0

    print(f"[*] Searching GitHub code for keyword: '{keyword}'")
    print(f"[*] Regex pattern: {pattern.pattern}")
    print(f"[*] Output directory: {output_dir.resolve()}\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(1, max_pages + 1):
            items = await fetch_search_page(client, keyword, page, headers)
            if items is None:
                break

            if not items:
                print(f"[*] No more results at page {page}. Done.")
                break

            print(f"[*] Page {page}: processing {len(items)} items ...")

            # Fan out file downloads concurrently within each page.
            tasks = [
                process_item(client, item, pattern, output_dir, headers, global_seen)
                for item in items
            ]
            results = await asyncio.gather(*tasks)

            page_tokens = sum(results)
            page_files = sum(1 for r in results if r > 0)
            total_tokens += page_tokens
            total_files += page_files

            print(f"    -> {page_files} files written, {page_tokens} unique tokens")

            # Respect rate limits: GitHub code-search allows ~10 req/min
            # for unauthenticated users. A short pause helps stay under.
            if page < max_pages:
                await asyncio.sleep(2.0)

    print(f"\n[*] Finished. {total_files} files created, {total_tokens} unique tokens total.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search GitHub code and extract keyword-matched tokens."
    )
    parser.add_argument(
        "keyword",
        help="Keyword to search for (e.g. 'skills', 'mcp').",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="GitHub personal access token. Falls back to GITHUB_TOKEN env var.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Maximum number of search-result pages to fetch (default: 5).",
    )

    args = parser.parse_args()

    if not args.token:
        print(
            "[!] No GitHub token provided. Unauthenticated requests are heavily "
            "rate-limited.\n    Set GITHUB_TOKEN in .env or pass --token.\n"
        )

    asyncio.run(run(args.keyword, args.token, args.max_pages))


if __name__ == "__main__":
    main()