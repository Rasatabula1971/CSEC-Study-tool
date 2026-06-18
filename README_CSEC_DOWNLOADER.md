# CSEC 2027 Resource Downloader

This tool builds a local CSEC 2027 resource folder by downloading files from an allow-listed set of public education sites and sorting them by subject/category.

It is designed for the subjects:

- Economics
- Mathematics
- English A
- Principles of Accounts (POA)
- Principles of Business (POB)
- Integrated Science
- Information Technology (I.T.)

## Important legal rule

Do **not** add random textbook PDF dump sites to the manifest. Keep the downloader restricted to official/free sources or resources you have permission to store. The script blocks common copyright-dump domains by default.

## Install

From the repo root:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r tools/requirements.txt
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r tools/requirements.txt
```

## Test first

```bash
python tools/csec_resource_downloader.py --dry-run
```

## Download and sort

```bash
python tools/csec_resource_downloader.py --manifest tools/csec_sources.json --out resources/raw/2027
```

The output structure will look like:

```text
resources/raw/2027/
  Economics/
    syllabus/
    notes/
    past_papers/
    mark_schemes_answer_keys/
  Mathematics/
  English_A/
  POA/
  POB/
  Integrated_Science/
  Information_Technology/
  _logs/
    downloads.jsonl
    seed_pages.jsonl
    review_needed.jsonl
    summary.json
```

## What gets downloaded automatically

- Direct approved syllabus PDFs from CXC.
- PDFs, DOCX, PPTX and text files linked from trusted seed pages, if they are on allow-listed domains.

## What does not get downloaded automatically

- Google Drive files from third-party sites.
- Scribd/PDFCoffee/Z-Library/LibGen/Anna’s Archive style files.
- Login-only resources.
- HTML notes as copied webpages. The script records scanned seed-page counts in `resources/raw/2027/_logs/seed_pages.jsonl` instead.

## Add new approved sources

Edit `tools/csec_sources.json`.

For a direct PDF:

```json
{
  "title": "Approved Free Book Title",
  "category": "textbooks_open_books",
  "url": "https://example.edu/free-book.pdf"
}
```

For a page to scan:

```json
{"url": "https://example.edu/csec-maths", "default_category": "notes"}
```

Also add the domain to `allowed_domains`.

## Uploading resources to GitHub

Be careful uploading downloaded papers/books to a public GitHub repo. Upload only resources that are clearly public-domain, official, open-licensed, or allowed by the owner. For questionable files, keep them local or private.
