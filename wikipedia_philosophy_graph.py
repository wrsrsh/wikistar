"""Build a dump-scale "Getting to Philosophy" graph on Modal.

Run ``modal run wikipedia_philosophy_graph.py::build`` after ``modal setup``.
See README.md for the required storage, cost, and output-download commands.
"""

from __future__ import annotations

import bz2
import csv
import gzip
import hashlib
import json
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import modal

from wiki_links import extract_first_article_link, normalise_title

APP_NAME = "wikipedia-philosophy-graph"
VOLUME_NAME = "wikipedia-philosophy-data"
VOLUME_MOUNT = "/data"
DUMPS_ROOT = Path(VOLUME_MOUNT) / "dumps"
RUNS_ROOT = Path(VOLUME_MOUNT) / "runs"
DEFAULT_DUMP_URL = (
    "https://dumps.wikimedia.org/enwiki/latest/"
    "enwiki-latest-pages-articles-multistream.xml.bz2"
)
PHILOSOPHY = "Philosophy"
MAX_TIMEOUT_SECONDS = 24 * 60 * 60

app = modal.App(APP_NAME)
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
cpu_image = modal.Image.debian_slim(python_version="3.12")
gpu_image = modal.Image.debian_slim(python_version="3.12").uv_pip_install(
    "numpy", "torch"
)


class PipelineError(RuntimeError):
    """A configuration or durable-pipeline precondition was not met."""


def _validated_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", run_id):
        raise PipelineError(
            "run_id must contain 1–81 letters, numbers, dots, underscores, or hyphens "
            "and must begin with a letter or number"
        )
    return run_id


def _run_dir(run_id: str) -> Path:
    return RUNS_ROOT / _validated_run_id(run_id)


def _dump_path(run_id: str) -> Path:
    return DUMPS_ROOT / f"{_validated_run_id(run_id)}.xml.bz2"


def _local_name(tag: str) -> str:
    """Strip the XML namespace from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _page_fields(page: ET.Element) -> tuple[str | None, str | None, bool]:
    """Extract title, revision text, and redirect status from a dump page node."""
    title: str | None = None
    text: str | None = None
    is_redirect = False
    for child in page:
        name = _local_name(child.tag)
        if name == "title":
            title = child.text
        elif name == "redirect":
            is_redirect = True
        elif name == "revision":
            for revision_child in child:
                if _local_name(revision_child.tag) == "text":
                    text = revision_child.text
                    break
    return title, text, is_redirect


def _iter_pages(dump: Path) -> Iterator[ET.Element]:
    """Yield pages while clearing the already-consumed XML root from memory."""
    with bz2.open(dump, "rb") as source:
        events = ET.iterparse(source, events=("start", "end"))
        _event, root = next(events)
        for event, element in events:
            if event == "end" and _local_name(element.tag) == "page":
                yield element
                # ``element.clear()`` alone leaves millions of empty <page>
                # children attached to <mediawiki>. Clearing the root prevents
                # that otherwise unbounded retained-memory growth.
                root.clear()


def _open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA temp_store = FILE")
    return connection


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _safe_graph_id(title: str) -> str:
    return "n" + hashlib.sha1(title.encode("utf-8")).hexdigest()


def _download(url: str, destination: Path, force: bool) -> dict[str, Any]:
    """Download a dump resumably to a local Modal Volume path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        return {
            "dump": str(destination),
            "downloaded": False,
            "bytes": destination.stat().st_size,
            "message": "Existing dump retained; pass --force to download again.",
        }

    partial = destination.with_suffix(destination.suffix + ".partial")
    already_downloaded = partial.stat().st_size if partial.exists() and not force else 0
    if force:
        partial.unlink(missing_ok=True)

    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
    if already_downloaded:
        request.add_header("Range", f"bytes={already_downloaded}-")

    with urllib.request.urlopen(request, timeout=60) as response:
        status = getattr(response, "status", response.getcode())
        # A server that ignores Range must not receive an append write.
        append = already_downloaded > 0 and status == 206
        if already_downloaded and not append:
            already_downloaded = 0
        mode = "ab" if append else "wb"
        content_length = response.headers.get("Content-Length")
        expected = already_downloaded + int(content_length) if content_length else None
        completed = already_downloaded
        next_log = completed + 1024**3
        with partial.open(mode) as output:
            while chunk := response.read(8 * 1024 * 1024):
                output.write(chunk)
                completed += len(chunk)
                if completed >= next_log:
                    print(f"Downloaded {completed / 1024**3:.1f} GiB", flush=True)
                    next_log += 1024**3

    if expected is not None and completed != expected:
        raise PipelineError(
            f"Incomplete download: expected {expected} bytes but wrote {completed} bytes"
        )
    partial.replace(destination)
    return {"dump": str(destination), "downloaded": True, "bytes": completed, "url": url}


@app.function(
    image=cpu_image,
    volumes={VOLUME_MOUNT: volume},
    timeout=MAX_TIMEOUT_SECONDS,
    retries=modal.Retries(max_retries=2, initial_delay=5.0, backoff_coefficient=2.0),
)
def download_dump(run_id: str, dump_url: str = DEFAULT_DUMP_URL, force: bool = False) -> dict[str, Any]:
    """Fetch the official Wikimedia pages-articles dump into the persistent Volume."""
    result = _download(dump_url, _dump_path(run_id), force)
    volume.commit()
    return result


@app.function(image=cpu_image, volumes={VOLUME_MOUNT: volume}, timeout=MAX_TIMEOUT_SECONDS)
def extract_first_links(
    run_id: str, max_pages: int = 0, overwrite: bool = False
) -> dict[str, Any]:
    """Stream the XML dump and persist one deterministic edge per source page."""
    _validated_run_id(run_id)
    if max_pages < 0:
        raise PipelineError("max_pages must be zero (all pages) or a positive integer")

    volume.reload()
    dump = _dump_path(run_id)
    if not dump.exists():
        raise PipelineError(f"Dump not found at {dump}; run the download entrypoint first")

    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    database_path = run_dir / "first_links.sqlite"
    if database_path.exists():
        if not overwrite:
            raise PipelineError(
                f"{database_path} already exists. Choose a new run_id or pass --overwrite."
            )
        database_path.unlink()

    started_at = time.monotonic()
    pages_seen = 0
    redirects_skipped = 0
    empty_or_non_article = 0
    edges_written = 0
    connection = _open_database(database_path)
    try:
        connection.execute(
            "CREATE TABLE first_links (title TEXT PRIMARY KEY, next_title TEXT NOT NULL) WITHOUT ROWID"
        )
        batch: list[tuple[str, str]] = []
        for page in _iter_pages(dump):
            pages_seen += 1
            title, text, is_redirect = _page_fields(page)
            source_title = normalise_title(title or "")
            if is_redirect:
                redirects_skipped += 1
            elif source_title is None or text is None:
                empty_or_non_article += 1
            else:
                next_title = extract_first_article_link(text)
                if next_title is None:
                    empty_or_non_article += 1
                else:
                    batch.append((source_title, next_title))
                    if len(batch) >= 25_000:
                        connection.executemany(
                            "INSERT OR REPLACE INTO first_links(title, next_title) VALUES (?, ?)", batch
                        )
                        connection.commit()
                        edges_written += len(batch)
                        batch.clear()
            if max_pages and pages_seen >= max_pages:
                break
            if pages_seen % 100_000 == 0:
                print(f"Parsed {pages_seen:,} pages; retained {edges_written + len(batch):,} edges", flush=True)

        if batch:
            connection.executemany(
                "INSERT OR REPLACE INTO first_links(title, next_title) VALUES (?, ?)", batch
            )
            connection.commit()
            edges_written += len(batch)
        connection.execute("CREATE INDEX first_links_next_title_idx ON first_links(next_title)")
        connection.commit()
    finally:
        connection.close()

    stats = {
        "run_id": run_id,
        "dump": str(dump),
        "pages_seen": pages_seen,
        "edges_written": edges_written,
        "redirects_skipped": redirects_skipped,
        "empty_or_non_article": empty_or_non_article,
        "max_pages": max_pages,
        "seconds": round(time.monotonic() - started_at, 2),
        "edge_definition": "first eligible main-namespace wikilink in lead prose, excluding templates, references, and balanced parentheses",
    }
    _write_json(run_dir / "parse_stats.json", stats)
    volume.commit()
    return stats


@app.function(image=cpu_image, volumes={VOLUME_MOUNT: volume}, timeout=MAX_TIMEOUT_SECONDS)
def solve_routes(run_id: str, overwrite: bool = False) -> dict[str, Any]:
    """Reverse-walk first-link edges to find every page that reaches Philosophy.

    The first-link graph is functional (at most one outgoing edge per page), so
    a reverse breadth-first search gives a single shortest deterministic route
    for every matching page without materialising every possible walk.
    """
    _validated_run_id(run_id)
    volume.reload()
    run_dir = _run_dir(run_id)
    database_path = run_dir / "first_links.sqlite"
    if not database_path.exists():
        raise PipelineError(f"Missing {database_path}; run extract_first_links first")

    started_at = time.monotonic()
    connection = _open_database(database_path)
    try:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='routes'"
        ).fetchone()
        if existing and not overwrite:
            raise PipelineError("Routes already exist; pass --overwrite to recalculate them")
        connection.execute("DROP TABLE IF EXISTS routes")
        connection.execute(
            "CREATE TABLE routes (title TEXT PRIMARY KEY, next_title TEXT, distance INTEGER NOT NULL) WITHOUT ROWID"
        )
        connection.execute(
            "INSERT INTO routes(title, next_title, distance) VALUES (?, NULL, 0)", (PHILOSOPHY,)
        )
        connection.commit()

        frontier = [PHILOSOPHY]
        distance = 0
        routes_found = 1
        while frontier:
            next_frontier: list[str] = []
            for offset in range(0, len(frontier), 900):
                titles = frontier[offset : offset + 900]
                placeholders = ",".join("?" for _ in titles)
                candidates = connection.execute(
                    f"SELECT title, next_title FROM first_links WHERE next_title IN ({placeholders})",
                    titles,
                )
                for title, next_title in candidates:
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO routes(title, next_title, distance) VALUES (?, ?, ?)",
                        (title, next_title, distance + 1),
                    )
                    if inserted.rowcount:
                        next_frontier.append(title)
            connection.commit()
            routes_found += len(next_frontier)
            distance += 1
            print(
                f"Solved distance {distance - 1}: frontier={len(frontier):,}; "
                f"routes={routes_found:,}",
                flush=True,
            )
            frontier = next_frontier
    finally:
        connection.close()

    stats = {
        "run_id": run_id,
        "target": PHILOSOPHY,
        "routes_found_including_target": routes_found,
        "max_distance": max(distance - 1, 0),
        "seconds": round(time.monotonic() - started_at, 2),
    }
    _write_json(run_dir / "route_stats.json", stats)
    volume.commit()
    return stats


@app.function(image=cpu_image, volumes={VOLUME_MOUNT: volume}, timeout=10 * 60)
def inspect_routes(run_id: str, limit: int = 50, max_distance: int = 10) -> list[dict[str, Any]]:
    """Return a bounded sample of pages that reach Philosophy for notebooks/UI."""
    _validated_run_id(run_id)
    if not 1 <= limit <= 1_000 or max_distance < 0:
        raise PipelineError("limit must be 1–1,000 and max_distance must be non-negative")
    volume.reload()
    database_path = _run_dir(run_id) / "first_links.sqlite"
    if not database_path.exists():
        raise PipelineError(f"Missing {database_path}; run solve_routes first")
    connection = _open_database(database_path)
    try:
        rows = connection.execute(
            "SELECT title, next_title, distance FROM routes "
            "WHERE distance <= ? ORDER BY distance, title LIMIT ?",
            (max_distance, limit),
        ).fetchall()
    finally:
        connection.close()
    return [
        {"title": title, "next_title": next_title, "distance_to_philosophy": distance}
        for title, next_title, distance in rows
    ]


@app.function(image=cpu_image, volumes={VOLUME_MOUNT: volume}, timeout=10 * 60)
def lookup_route(run_id: str, title: str, max_hops: int = 200) -> dict[str, Any]:
    """Return the deterministic first-link route from one page to Philosophy."""
    _validated_run_id(run_id)
    if not 1 <= max_hops <= 10_000:
        raise PipelineError("max_hops must be between 1 and 10,000")
    source = normalise_title(title)
    if source is None:
        raise PipelineError("title must be a main-namespace Wikipedia article title")
    volume.reload()
    database_path = _run_dir(run_id) / "first_links.sqlite"
    if not database_path.exists():
        raise PipelineError(f"Missing {database_path}; run solve_routes first")
    connection = _open_database(database_path)
    try:
        initial = connection.execute(
            "SELECT distance FROM routes WHERE title = ?", (source,)
        ).fetchone()
        if initial is None:
            return {
                "title": source,
                "reaches_philosophy": False,
                "reason": "No route to Philosophy under this run's first-link rule.",
                "path": [],
            }
        path = [source]
        current = source
        while len(path) <= max_hops:
            row = connection.execute(
                "SELECT next_title FROM routes WHERE title = ?", (current,)
            ).fetchone()
            if row is None or row[0] is None:
                break
            current = row[0]
            path.append(current)
        truncated = path[-1] != PHILOSOPHY
    finally:
        connection.close()
    return {
        "title": source,
        "reaches_philosophy": not truncated,
        "distance_to_philosophy": initial[0],
        "path": path,
        "truncated": truncated,
    }


@app.function(image=cpu_image, volumes={VOLUME_MOUNT: volume}, timeout=MAX_TIMEOUT_SECONDS)
def export_graph(run_id: str) -> dict[str, Any]:
    """Stream route CSV and GraphML artifacts without loading the graph into RAM."""
    _validated_run_id(run_id)
    volume.reload()
    run_dir = _run_dir(run_id)
    database_path = run_dir / "first_links.sqlite"
    if not database_path.exists():
        raise PipelineError(f"Missing {database_path}; run solve_routes first")

    csv_path = run_dir / "philosophy_routes.csv.gz"
    graphml_path = run_dir / "philosophy_routes.graphml.gz"
    connection = _open_database(database_path)
    try:
        route_count = connection.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        with gzip.open(csv_path, "wt", encoding="utf-8", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(("source_title", "target_title", "distance_to_philosophy"))
            for title, next_title, distance in connection.execute(
                "SELECT title, next_title, distance FROM routes ORDER BY distance, title"
            ):
                writer.writerow((title, next_title or "", distance))

        with gzip.open(graphml_path, "wt", encoding="utf-8", newline="") as graphml:
            graphml.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n'
                '  <key id="label" for="node" attr.name="label" attr.type="string"/>\n'
                '  <key id="distance" for="node" attr.name="distance_to_philosophy" attr.type="int"/>\n'
                '  <graph id="philosophy-routes" edgedefault="directed">\n'
            )
            for title, _next_title, distance in connection.execute(
                "SELECT title, next_title, distance FROM routes ORDER BY title"
            ):
                graphml.write(
                    f'    <node id="{_safe_graph_id(title)}"><data key="label">{escape(title)}'
                    f"</data><data key=\"distance\">{distance}</data></node>\n"
                )
            for title, next_title in connection.execute(
                "SELECT title, next_title FROM routes WHERE next_title IS NOT NULL ORDER BY title"
            ):
                graphml.write(
                    f'    <edge source="{_safe_graph_id(title)}" '
                    f'target="{_safe_graph_id(next_title)}"/>\n'
                )
            graphml.write("  </graph>\n</graphml>\n")
    finally:
        connection.close()

    result = {
        "run_id": run_id,
        "nodes": route_count,
        "route_csv": str(csv_path),
        "graphml": str(graphml_path),
    }
    _write_json(run_dir / "export_stats.json", result)
    volume.commit()
    return result


def _scale_svg_point(value: float, minimum: float, maximum: float, size: int, margin: int) -> float:
    if maximum == minimum:
        return size / 2
    return margin + (value - minimum) * (size - 2 * margin) / (maximum - minimum)


@app.function(
    image=gpu_image,
    gpu="T4",
    volumes={VOLUME_MOUNT: volume},
    timeout=60 * 60,
)
def render_gpu_overview(
    run_id: str, max_nodes: int = 2_000, max_distance: int = 8, iterations: int = 300
) -> dict[str, Any]:
    """Use a Modal GPU to make a force-directed SVG of the graph's core.

    A full Wikipedia-derived graph cannot be rendered legibly. This deliberately
    lays out the nodes nearest Philosophy; the complete CSV/GraphML remains the
    authoritative graph artifact.
    """
    if not 2 <= max_nodes <= 3_000:
        raise PipelineError("max_nodes must be between 2 and 3,000 for the dense GPU layout")
    if max_distance < 1 or iterations < 1:
        raise PipelineError("max_distance and iterations must be positive")

    import torch

    _validated_run_id(run_id)
    volume.reload()
    run_dir = _run_dir(run_id)
    database_path = run_dir / "first_links.sqlite"
    if not database_path.exists():
        raise PipelineError(f"Missing {database_path}; run solve_routes first")
    if not torch.cuda.is_available():
        raise PipelineError("CUDA is unavailable; this function must be scheduled with a Modal GPU")

    connection = _open_database(database_path)
    try:
        rows = list(
            connection.execute(
                "SELECT title, next_title, distance FROM routes "
                "WHERE distance <= ? ORDER BY distance, title LIMIT ?",
                (max_distance, max_nodes),
            )
        )
    finally:
        connection.close()
    if len(rows) < 2:
        raise PipelineError("The selected route graph has fewer than two nodes to lay out")

    titles = [row[0] for row in rows]
    title_to_index = {title: index for index, title in enumerate(titles)}
    distances = [row[2] for row in rows]
    edges = [
        (title_to_index[title], title_to_index[next_title])
        for title, next_title, _distance in rows
        if next_title in title_to_index
    ]
    device = torch.device("cuda")
    torch.manual_seed(7)
    node_count = len(titles)
    positions = torch.rand((node_count, 2), device=device) * 2 - 1
    source_indices = torch.tensor([edge[0] for edge in edges], dtype=torch.long, device=device)
    target_indices = torch.tensor([edge[1] for edge in edges], dtype=torch.long, device=device)
    ideal_length = 1.5 / max(node_count**0.5, 1)

    for step in range(iterations):
        difference = positions[:, None, :] - positions[None, :, :]
        squared_distance = difference.square().sum(dim=2).clamp_min(1e-4)
        repulsion = (difference / squared_distance.unsqueeze(2)).sum(dim=1) * (ideal_length**2)
        displacement = repulsion
        if edges:
            edge_difference = positions[source_indices] - positions[target_indices]
            edge_distance = edge_difference.norm(dim=1).clamp_min(1e-4)
            attraction = edge_difference * (edge_distance / ideal_length).unsqueeze(1)
            displacement.index_add_(0, source_indices, -attraction)
            displacement.index_add_(0, target_indices, attraction)
        cooling = 0.08 * (1 - step / iterations) + 0.002
        positions += displacement / displacement.norm(dim=1, keepdim=True).clamp_min(1e-4) * cooling
        positions.clamp_(-2.0, 2.0)

    output_positions = positions.detach().cpu().numpy()
    del positions
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    layout_csv = run_dir / "philosophy_overview_layout.csv"
    svg_path = run_dir / "philosophy_overview.svg"
    with layout_csv.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("title", "x", "y", "distance_to_philosophy"))
        for title, point, distance in zip(titles, output_positions, distances, strict=True):
            writer.writerow((title, f"{point[0]:.7f}", f"{point[1]:.7f}", distance))

    width, height, margin = 1_600, 1_100, 50
    x_min, y_min = output_positions.min(axis=0)
    x_max, y_max = output_positions.max(axis=0)
    with svg_path.open("w", encoding="utf-8") as svg:
        svg.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n<rect width="100%" height="100%" fill="#10131a"/>\n'
        )
        for source, target in edges:
            source_point, target_point = output_positions[source], output_positions[target]
            svg.write(
                '<line stroke="#64748b" stroke-opacity="0.22" stroke-width="1" '
                f'x1="{_scale_svg_point(source_point[0], x_min, x_max, width, margin):.2f}" '
                f'y1="{_scale_svg_point(source_point[1], y_min, y_max, height, margin):.2f}" '
                f'x2="{_scale_svg_point(target_point[0], x_min, x_max, width, margin):.2f}" '
                f'y2="{_scale_svg_point(target_point[1], y_min, y_max, height, margin):.2f}"/>\n'
            )
        max_seen_distance = max(distances) or 1
        for title, point, distance in zip(titles, output_positions, distances, strict=True):
            hue = int(210 - 170 * distance / max_seen_distance)
            radius = 6 if title == PHILOSOPHY else 2.5
            svg.write(
                f'<circle cx="{_scale_svg_point(point[0], x_min, x_max, width, margin):.2f}" '
                f'cy="{_scale_svg_point(point[1], y_min, y_max, height, margin):.2f}" r="{radius}" '
                f'fill="hsl({hue} 85% 65%)"><title>{escape(title)}</title></circle>\n'
            )
        svg.write("</svg>\n")

    result = {
        "run_id": run_id,
        "nodes_laid_out": node_count,
        "edges_laid_out": len(edges),
        "gpu": torch.cuda.get_device_name(0),
        "layout_csv": str(layout_csv),
        "svg": str(svg_path),
    }
    _write_json(run_dir / "layout_stats.json", result)
    volume.commit()
    return result


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("enwiki-%Y%m%dT%H%M%SZ")


@app.local_entrypoint()
def build(
    run_id: str = "",
    dump_url: str = DEFAULT_DUMP_URL,
    force_download: bool = False,
    max_pages: int = 0,
    overwrite: bool = False,
    render_overview: bool = True,
    layout_max_nodes: int = 2_000,
) -> None:
    """Download, parse, solve, export, and optionally GPU-render one full run."""
    run_id = run_id or _new_run_id()
    print(f"Starting run {run_id}")
    print(json.dumps(download_dump.remote(run_id, dump_url, force_download), indent=2))
    print(json.dumps(extract_first_links.remote(run_id, max_pages, overwrite), indent=2))
    print(json.dumps(solve_routes.remote(run_id, overwrite), indent=2))
    print(json.dumps(export_graph.remote(run_id), indent=2))
    if render_overview:
        print(json.dumps(render_gpu_overview.remote(run_id, layout_max_nodes), indent=2))
    print(f"Finished. Download outputs with: modal volume get {VOLUME_NAME} runs/{run_id} ./output")


@app.local_entrypoint()
def download(
    run_id: str = "", dump_url: str = DEFAULT_DUMP_URL, force: bool = False
) -> None:
    """Only download (or resume downloading) the Wikimedia dump."""
    run_id = run_id or _new_run_id()
    print(json.dumps(download_dump.remote(run_id, dump_url, force), indent=2))


@app.local_entrypoint()
def parse(run_id: str, max_pages: int = 0, overwrite: bool = False) -> None:
    """Only parse a previously downloaded dump into the durable edge database."""
    print(json.dumps(extract_first_links.remote(run_id, max_pages, overwrite), indent=2))


@app.local_entrypoint()
def analyze_existing(
    run_id: str,
    overwrite: bool = False,
    render_overview: bool = True,
    layout_max_nodes: int = 2_000,
) -> None:
    """Solve/export a previously parsed run, without downloading or parsing again."""
    print(json.dumps(solve_routes.remote(run_id, overwrite), indent=2))
    print(json.dumps(export_graph.remote(run_id), indent=2))
    if render_overview:
        print(json.dumps(render_gpu_overview.remote(run_id, layout_max_nodes), indent=2))
