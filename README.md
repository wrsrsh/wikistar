# Wikipedia → Philosophy graph on Modal

This project builds a **dump-scale English Wikipedia** graph on [Modal](https://modal.com). It uses the official `enwiki-latest-pages-articles-multistream.xml.bz2` dump rather than crawling Wikipedia's live site. That is both friendlier to Wikimedia and practical for millions of pages.

Each article has at most one outgoing edge: its first eligible main-namespace wikilink in lead prose. The pipeline then reverse-walks those edges from **Philosophy**. A page is in the output exactly when repeatedly following that edge reaches `Philosophy`; its `distance_to_philosophy` and complete deterministic route are retained.

> This is the classic *Getting to Philosophy* interpretation, not a claim that every possible Wikipedia hyperlink path is enumerated. Enumerating all paths is unbounded because the link graph has cycles and branching. The exact edge rule is recorded in `parse_stats.json`.

## What it creates

The persistent Modal Volume `wikipedia-philosophy-data` has one directory per run:

- `dumps/<run-id>.xml.bz2` — official input dump, retained to make runs repeatable.
- `runs/<run-id>/first_links.sqlite` — compact, queryable graph database; one first-link edge per article plus its solved routes.
- `runs/<run-id>/philosophy_routes.csv.gz` — `source_title,target_title,distance_to_philosophy` for every article that reaches Philosophy.
- `runs/<run-id>/philosophy_routes.graphml.gz` — the same directed route graph for Gephi, Cytoscape, or other GraphML readers.
- `runs/<run-id>/philosophy_overview.svg` and `philosophy_overview_layout.csv` — a GPU force-directed overview of a bounded neighborhood around Philosophy, if enabled.
- `*_stats.json` — parsing, route, export, and GPU-layout summaries.

`Philosophy` has distance zero. Each other CSV/GraphML edge points one step closer to it.

## Before running

1. Install and authenticate the Modal CLI (as shown in the Modal dashboard):

   ```bash
   pip install -r requirements.txt
   python3 -m modal setup
   ```

2. Expect substantial storage and compute use. The current English pages-articles dump is many GiB compressed, and the job also keeps a SQLite database and compressed outputs. Start with **at least 50 GiB** available in your Modal Volume and monitor the Modal dashboard. Parsing all pages can take many hours; a Modal function is configured with its maximum 24-hour timeout.

3. Use a meaningful immutable run ID. It makes downloaded dumps and outputs easy to find. A full production run should not set `--max-pages`.

## Run the full pipeline

```bash
modal run wikipedia_philosophy_graph.py::build \
  --run-id enwiki-latest-2026-08 \
  --layout-max-nodes 2000
```

The command:

1. resumes or downloads the current official dump into the Volume,
2. streams the compressed XML on a CPU worker and extracts first links,
3. reverse-solves every chain that reaches Philosophy,
4. exports CSV and GraphML, and
5. schedules `render_gpu_overview` on a **T4 Modal GPU** for the bounded SVG overview.

The GPU is used for force-directed layout, where it provides real parallel acceleration. XML decompression, MediaWiki-text filtering, and SQLite graph traversal are intentionally CPU jobs: using a GPU for them would add cost without accelerating the work.

### Cheap smoke test

Verify credentials and the parser before spending on the full dump:

```bash
modal run wikipedia_philosophy_graph.py::build \
  --run-id smoke-test \
  --max-pages 50000 \
  --layout-max-nodes 500
```

This is an incomplete prefix of the dump, so its coverage is **not** a valid full-Wikipedia statistic. It is only a pipeline test.

### Run in durable phases

A completed dump can be reused across attempts. These commands make failures or deliberate inspection between phases easier:

```bash
# Download or resume the dump.
modal run wikipedia_philosophy_graph.py::download --run-id enwiki-latest-2026-08

# Parse all pages. --overwrite discards an existing first_links.sqlite for this run.
modal run wikipedia_philosophy_graph.py::parse --run-id enwiki-latest-2026-08

# Solve paths, create artifacts, and render the GPU overview.
modal run wikipedia_philosophy_graph.py::analyze_existing \
  --run-id enwiki-latest-2026-08
```

Use `--force` with `::download` to redownload a completed dump, and `--overwrite` with parsing/solving only when intentionally rebuilding that phase. The downloader uses a `.partial` file and HTTP Range requests, so an interrupted download resumes when the origin permits it.

## Retrieve and inspect results

```bash
modal volume ls wikipedia-philosophy-data runs/enwiki-latest-2026-08
modal volume get wikipedia-philosophy-data runs/enwiki-latest-2026-08 ./output
```

The GraphML can be too large to render as one meaningful visual graph. Load it in a graph tool with filtering by `distance_to_philosophy`; use the SVG as a compact overview. The route database is the source of truth for per-page paths.

For example, after deploying the app, the provided notebook can ask for one route:

```python
import modal
route_for = modal.Function.from_name("wikipedia-philosophy-graph", "lookup_route")
route_for.remote("enwiki-latest-2026-08", "Artificial intelligence")
```

## Modal Notebook

`notebooks/explore_wikipedia_philosophy.ipynb` is a lightweight exploration notebook. First make the function names durable:

```bash
modal deploy wikipedia_philosophy_graph.py
modal launch jupyter --gpu a10g
```

Open the resulting Jupyter URL and upload/open the notebook. It calls the deployed CPU query functions (`inspect_routes` and `lookup_route`), so it does not copy the multi-GiB dump into the notebook. The GPU launched for Jupyter is optional—the pipeline already uses a T4 for its overview; keep it only if you want to do your own GPU analysis in notebook cells. You can also upload the notebook directly at [modal.com/notebooks](https://modal.com/notebooks).

## Edge-definition details and limits

The extractor is intentionally dependency-free and works in a streaming pass. For each non-redirect main-namespace page, it:

1. reads only the lead (before the first level-2 heading),
2. excludes comments, `<ref>` blocks, balanced templates, and balanced parenthetical text,
3. scans wikilinks in text order,
4. skips non-article namespace/interwiki/file/category targets, and
5. selects the first remaining target.

That rule is deterministic and scalable, but it is not a full MediaWiki renderer. In particular, complicated malformed markup, HTML-generated links, and nuanced template semantics can differ from the web UI. The output should therefore be cited as an analysis under this stated rule and dump version, not as an authoritative statement about every rendered Wikipedia link.

## Local checks

The pure parsing helpers can be checked without a Modal account:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile wiki_links.py wikipedia_philosophy_graph.py
```
