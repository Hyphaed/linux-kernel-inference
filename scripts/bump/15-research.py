#!/usr/bin/env python3
"""Scripted research delta for a kernel bump (ADR-009). Writes out/sweep/<VER>/research-delta.md.

Queries arXiv, Semantic Scholar, OpenAlex, lore.kernel.org and the NVIDIA open-module releases for
items newer than the previous bump's research date, tagged [seen] when an id already appears in
docs/research/*.md. A source that fails warns and is skipped; the stage never fails. The query set
mirrors workflow/05_RESEARCH_PLAYBOOK.md. Prints TIER=full when the major.minor changed since the
last recorded run or RESEARCH=full is set, which means: run /hyperresearch, don't rely on this file.

`--commit-state` records today's date and VER's major.minor as the new baseline.
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser()
ap.add_argument("--ver", default=os.environ.get("VER"), required=not os.environ.get("VER"))
ap.add_argument("--commit-state", action="store_true")
ap.add_argument("--since", help="YYYY-MM-DD override of the baseline date")
a = ap.parse_args()
ver = a.ver; mm = ver.rsplit(".", 1)[0]
out = ROOT / "out" / "sweep" / ver; out.mkdir(parents=True, exist_ok=True)
state_f = ROOT / "out" / "sweep" / "research-state.json"
state = json.loads(state_f.read_text()) if state_f.exists() else {}
today = dt.date.today().isoformat()
since = a.since or state.get("date") or (dt.date.today() - dt.timedelta(days=45)).isoformat()
tier_full = os.environ.get("RESEARCH") == "full" or state.get("mm") != mm

# Topology: 8P+16E hybrid (ITMT/HFI), 1 NUMA node, RTX 5070 on a Gen4 host link, NVMe, r8169.
QUERIES = {
    "sched-hybrid": "Linux scheduler hybrid cores",
    "sched-ext": "sched_ext scheduler",
    "cpufreq-intel": "intel_pstate Linux",
    "mm-pagecache": "Linux page cache eviction",
    "mm-thp-tier": "huge pages Linux tiering",
    "gpu-dma": "LLM inference offload PCIe",
    "nvme-io": "NVMe io_uring Linux GPU",
    "net-stream": "game streaming latency Linux",
}
SUBSYS = ["sched", "x86", "intel_pstate", "mm", "dma-buf", "nvme", "pci"]

seen_ids = set()
for d in (ROOT / "docs" / "research").glob("*.md"):
    t = d.read_text(errors="ignore")
    seen_ids |= set(re.findall(r"\b\d{4}\.\d{4,5}\b", t)) | set(re.findall(r"\b10\.\d{4,9}/\S+?(?=[\s)\]>,;]|$)", t))

def get(url, tries=2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hyphaed-bump-research/1 (local kernel build)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            err = e; time.sleep(2 * (i + 1))
    print(f"warn: {url[:90]}: {err}", file=sys.stderr)
    return None

sections = []
def mark(i): return " [seen]" if i in seen_ids else ""

def arxiv():
    rows = []
    for k, q in QUERIES.items():
        # every term must match, in a systems/architecture/perf category; bare "all:a b c" is a noisy OR
        terms = " AND ".join("all:" + w for w in q.split())
        cats = "(cat:cs.OS OR cat:cs.DC OR cat:cs.AR OR cat:cs.PF OR cat:cs.NI)"
        url = ("https://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(f"{cats} AND ({terms})") +
               "&sortBy=submittedDate&sortOrder=descending&max_results=15")
        b = get(url); time.sleep(3)  # arXiv asks for 3 s between calls
        if not b: continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in ET.fromstring(b).findall("a:entry", ns):
            pub = e.find("a:published", ns).text[:10]
            if pub < since: continue
            i = e.find("a:id", ns).text.rsplit("/", 1)[-1].split("v")[0]
            t = " ".join(e.find("a:title", ns).text.split())
            rows.append(f"- {i} ({pub}) {k}: {t}{mark(i)}")
    return sorted(set(rows))

def s2():
    rows = []
    for k, q in QUERIES.items():
        url = ("https://api.semanticscholar.org/graph/v1/paper/search?query=" + urllib.parse.quote(q) +
               "&fields=title,year,citationCount,externalIds,publicationDate&limit=10&publicationDateOrYear=" + since + ":")
        b = get(url, tries=1); time.sleep(1.2)
        if not b:
            break  # unauthenticated S2 is rate-limited hard (429); one failure means the rest will fail too
        for p in json.loads(b).get("data", []):
            ids = p.get("externalIds") or {}
            i = ids.get("ArXiv") or ids.get("DOI") or p["paperId"]
            rows.append(f"- {i} ({p.get('publicationDate')}, cited {p.get('citationCount')}) {k}: {p['title']}{mark(i)}")
    return sorted(set(rows))

def openalex():
    rows = []
    for k, q in QUERIES.items():
        url = ("https://api.openalex.org/works?search=" + urllib.parse.quote(q) +
               f"&filter=from_publication_date:{since}&sort=cited_by_count:desc&per-page=8&mailto=research@example.com")
        b = get(url)
        if not b: continue
        for w in json.loads(b).get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            rows.append(f"- {doi or w['id']} ({w.get('publication_date')}, cited {w.get('cited_by_count')}) {k}: {w.get('title')}{mark(doi)}")
    return sorted(set(rows))

def lore():
    rows = []
    for s in SUBSYS:
        url = "https://lore.kernel.org/all/?x=A&q=" + urllib.parse.quote(f"s:PATCH s:{s} d:{since}.. rt:{since}..")
        b = get(url); time.sleep(1)
        if not b: continue
        try: root = ET.fromstring(b)
        except ET.ParseError: continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns)[:15]:
            t = " ".join((e.find("a:title", ns).text or "").split())
            if t.lower().startswith("re:"): continue  # replies; the thread's first mail is listed on its own
            link = e.find("a:link", ns).get("href")
            rows.append(f"- {s}: {t} <{link}>")
    return rows

def nvidia():
    b = get("https://api.github.com/repos/NVIDIA/open-gpu-kernel-modules/releases?per_page=5")
    if not b: return []
    return [f"- {r['tag_name']} ({r['published_at'][:10]}) {r['html_url']}" for r in json.loads(b)]

for name, fn in (("arXiv", arxiv), ("Semantic Scholar", s2), ("OpenAlex", openalex),
                 ("lore.kernel.org (PATCH mail)", lore), ("NVIDIA open-gpu-kernel-modules releases", nvidia)):
    rows = fn()
    new = [r for r in rows if not r.endswith("[seen]")]
    sections.append(f"## {name}: {len(rows)} items, {len(new)} not seen before\n\n" + ("\n".join(rows) or "_nothing, or source unreachable (see stderr)_"))

hdr = (f"# Research delta for {ver}\n\nWindow: since {since} (today {today}). Baseline minor: {state.get('mm', 'none')}.\n"
       f"Tier: **{'FULL: run /hyperresearch, this file is only the seed' if tier_full else 'delta'}**.\n"
       "Titles only. Reading them and writing verdicts is the human step (docs/research/<VER>-patch-sweep-*.md).\n")
(out / "research-delta.md").write_text(hdr + "\n" + "\n\n".join(sections) + "\n")
print(f"wrote {out / 'research-delta.md'}")
print("TIER=full" if tier_full else "TIER=delta")
if a.commit_state:
    state.update(date=today, mm=mm, ver=ver); state_f.write_text(json.dumps(state, indent=1)); print("research state recorded")
