#!/usr/bin/env python3
"""Download and sort approved CSEC 2027 study resources.

This is an allow-list downloader, not a piracy scraper. It downloads direct
approved files and file links found on approved seed pages, then sorts them by
subject/category. Questionable links are logged for review instead of downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Install dependencies first: pip install -r tools/requirements.txt", file=sys.stderr)
    raise

USER_AGENT = "CSEC-Study-tool-resource-downloader/1.0 (+educational personal archive)"
FILE_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".txt"}
BLOCKED_DOMAINS = {
    "scribd.com", "www.scribd.com", "pdfcoffee.com", "www.pdfcoffee.com",
    "z-lib.org", "www.z-lib.org", "libgen.is", "libgen.rs", "annas-archive.org"
}
CATEGORY_WORDS = {
    "syllabus": ["syllabus", "syllabi"],
    "specimen_papers": ["specimen"],
    "mark_schemes_answer_keys": ["mark scheme", "marking scheme", "answer key", "answers", "solutions", "solution"],
    "past_papers": ["past paper", "past papers", "paper 1", "paper 01", "paper 2", "paper 02", "paper 3", "paper 03", "january", "may", "june"],
    "textbooks_open_books": ["textbook", "book", "mep", "openstax", "ck-12"],
    "notes": ["notes", "worksheet", "study guide", "guide", "lesson"],
}
SUBJECT_WORDS = {
    "Economics": ["economics", "econ"],
    "Mathematics": ["mathematics", "maths", "math"],
    "English_A": ["english a", "english-a", "english_a", "english"],
    "POA": ["principles of accounts", "poa", "accounts", "accounting"],
    "POB": ["principles of business", "pob", "business"],
    "Integrated_Science": ["integrated science", "int sci", "inti sci", "science"],
    "Information_Technology": ["information technology", "info tech", "computer", " it "],
}


def host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def is_blocked(url: str) -> bool:
    h = host(url)
    return h in BLOCKED_DOMAINS or any(h.endswith("." + d) for d in BLOCKED_DOMAINS)


def clean_text(text: str) -> str:
    return " " + (text or "").lower().replace("_", " ").replace("-", " ") + " "


def classify_category(text: str, default: str) -> str:
    hay = clean_text(text)
    for category, words in CATEGORY_WORDS.items():
        if any(w in hay for w in words):
            return category
    return default


def infer_subject(text: str, fallback: str) -> str:
    hay = clean_text(text)
    for subject, words in SUBJECT_WORDS.items():
        if any(w in hay for w in words):
            return subject
    return fallback


def is_file_url(url: str) -> bool:
    return Path(urlparse(url).path.lower()).suffix in FILE_EXTS


def slugify(text: str) -> str:
    text = unquote(text or "resource")
    text = re.sub(r"[^A-Za-z0-9._ -]+", " ", text)
    text = re.sub(r"\s+", "_", text.strip().lower())
    text = re.sub(r"_+", "_", text).strip("._-")
    return text[:140] or "resource"


def safe_filename(url: str, title: str = "", content_type: str = "") -> str:
    name = Path(unquote(urlparse(url).path)).name or title or "resource"
    suffix = Path(name).suffix.lower()
    if suffix not in FILE_EXTS:
        suffix = ".pdf" if "pdf" in content_type.lower() else ".bin"
    return slugify(Path(name).stem) + suffix


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def robots_allowed(session: requests.Session, url: str, ua: str, cache: dict[str, RobotFileParser]) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    if robots_url not in cache:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            resp = session.get(robots_url, timeout=15)
            rp.parse(resp.text.splitlines() if resp.ok else [])
        except requests.RequestException:
            rp.parse([])
        cache[robots_url] = rp
    return cache[robots_url].can_fetch(ua, url)


def download(session: requests.Session, item: dict, out: Path, dry_run: bool, overwrite: bool) -> dict:
    dest_dir = out / item["subject"] / item["category"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    record = dict(item)
    if dry_run:
        record.update({"status": "planned", "path": str(dest_dir / safe_filename(item["url"], item.get("title", "")))})
        return record
    try:
        with session.get(item["url"], stream=True, timeout=60) as resp:
            resp.raise_for_status()
            dest = dest_dir / safe_filename(item["url"], item.get("title", ""), resp.headers.get("content-type", ""))
            if dest.exists() and not overwrite:
                record.update({"status": "exists", "path": str(dest), "sha256": sha256_file(dest)})
                return record
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
            tmp.replace(dest)
            record.update({"status": "downloaded", "path": str(dest), "bytes": dest.stat().st_size, "sha256": sha256_file(dest)})
            return record
    except Exception as exc:
        record.update({"status": "error", "error": str(exc)})
        return record


def scan_seed(session: requests.Session, seed: dict, subject: str, manifest: dict, logs: Path) -> list[dict]:
    seed_url = seed["url"]
    allowed = set(manifest["allowed_domains"])
    allow_external = bool(manifest.get("allow_external_files", False))
    resp = session.get(seed_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    candidates = []
    index_rows = []
    for a in soup.find_all("a", href=True):
        url = urljoin(seed_url, a["href"].strip())
        text = a.get_text(" ", strip=True)
        combined = f"{url} {text} {title}"
        row = {
            "source_page": seed_url,
            "url": url,
            "text": text,
            "subject_guess": infer_subject(combined, subject),
            "category_guess": classify_category(combined, seed.get("default_category", "source_pages")),
        }
        index_rows.append(row)
        if is_blocked(url) or not is_file_url(url):
            continue
        if host(url) not in allowed and not allow_external:
            continue
        candidates.append({
            "subject": row["subject_guess"],
            "category": row["category_guess"],
            "url": url,
            "source_page": seed_url,
            "title": text or title,
        })
    append_jsonl(logs / "seed_pages.jsonl", {"seed": seed_url, "links_found": len(index_rows), "download_candidates": len(candidates)})
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="tools/csec_sources.json")
    parser.add_argument("--out", default="resources/raw/2027")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--respect-robots", action="store_true")
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    out = Path(args.out)
    logs = out / "_logs"
    ua = manifest.get("user_agent", USER_AGENT)
    session = requests.Session()
    session.headers.update({"User-Agent": ua})
    robots_cache: dict[str, RobotFileParser] = {}
    records = []

    def allowed_by_robots(url: str) -> bool:
        return True if not args.respect_robots else robots_allowed(session, url, ua, robots_cache)

    for subject in manifest.get("subjects", []):
        subject_name = subject["name"]
        for direct in subject.get("direct_files", []):
            url = direct["url"]
            item = {
                "subject": subject_name,
                "category": direct.get("category", classify_category(url, "source_pages")),
                "url": url,
                "source_page": direct.get("source_page", url),
                "title": direct.get("title", ""),
            }
            if is_blocked(url) or not allowed_by_robots(url):
                append_jsonl(logs / "review_needed.jsonl", {"reason": "blocked_or_robots", **item})
                continue
            rec = download(session, item, out, args.dry_run, args.overwrite)
            append_jsonl(logs / "downloads.jsonl", rec)
            records.append(rec)
            time.sleep(args.sleep)

        for seed in subject.get("seed_pages", []):
            seed_url = seed["url"]
            if is_blocked(seed_url) or host(seed_url) not in set(manifest["allowed_domains"]) or not allowed_by_robots(seed_url):
                append_jsonl(logs / "review_needed.jsonl", {"reason": "seed_blocked_or_not_allowed", "subject": subject_name, "url": seed_url})
                continue
            try:
                for item in scan_seed(session, seed, subject_name, manifest, logs):
                    if not allowed_by_robots(item["url"]):
                        append_jsonl(logs / "review_needed.jsonl", {"reason": "robots_file", **item})
                        continue
                    rec = download(session, item, out, args.dry_run, args.overwrite)
                    append_jsonl(logs / "downloads.jsonl", rec)
                    records.append(rec)
                    time.sleep(args.sleep)
            except Exception as exc:
                append_jsonl(logs / "review_needed.jsonl", {"reason": "seed_error", "subject": subject_name, "url": seed_url, "error": str(exc)})
            time.sleep(args.sleep)

    summary = {
        "dry_run": args.dry_run,
        "output_root": str(out),
        "records": len(records),
        "downloaded": sum(r.get("status") == "downloaded" for r in records),
        "existing": sum(r.get("status") == "exists" for r in records),
        "planned": sum(r.get("status") == "planned" for r in records),
        "errors": sum(r.get("status") == "error" for r in records),
        "logs": str(logs),
    }
    write_json(logs / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
