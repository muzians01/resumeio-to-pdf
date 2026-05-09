import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests
from fastapi import HTTPException

logger = logging.getLogger(__name__)

RESUMEIO_BASE = "https://resume.io"
WORKER_CACHE_DIR = Path(__file__).parent.parent / "renderer" / ".worker_cache"
RENDER_SCRIPT = Path(__file__).parent.parent / "renderer" / "render.mjs"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class ResumeioRenderer:
    """Render a resume.io document JSON as a multi-page PDF.

    Uses resume.io's own rendering Web Worker (run via Node.js) to produce
    a pixel-perfect PDF from the resume document JSON and renderer config.

    Parameters
    ----------
    document : dict
        The full resume document JSON from resume.io.
    """

    document: dict

    def generate_pdf(self) -> bytes:
        """Generate a PDF from the resume document.

        Returns
        -------
        bytes
            PDF file contents.
        """
        locale = self.document.get("locale", "en")
        config = self._get_renderer_config(locale)
        self._ensure_worker_assets()
        return self._render(self.document, config)

    def _get_renderer_config(self, locale: str) -> dict:
        """Fetch the renderer configuration for the given locale."""
        response = requests.get(
            f"{RESUMEIO_BASE}/api/app/general/renderer-config/{locale}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch renderer config")
        return response.json()

    def _ensure_worker_assets(self) -> None:
        """Download and cache the rendering Web Worker and its vendor chunks."""
        if (WORKER_CACHE_DIR / "rendering.js").exists():
            return

        logger.info("Downloading Web Worker assets from resume.io...")
        WORKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Find builder JS from the app page
        html = self._get_public(f"{RESUMEIO_BASE}/app/resumes")
        builder_match = re.search(r'/assets/js/builder-[^"\']+\.js', html)
        if not builder_match:
            raise HTTPException(status_code=502, detail="Could not find builder JS on resume.io")
        builder_url = f"{RESUMEIO_BASE}{builder_match.group()}"

        # 2. Find the rendering-core chunk hash from builder JS
        builder_js = self._get_public(builder_url)
        core_id_match = re.search(r'\{(\d+):"rendering-core"', builder_js)
        if not core_id_match:
            raise HTTPException(status_code=502, detail="Could not find rendering-core chunk ID")
        core_id = core_id_match.group(1)

        hash_match = re.search(rf'\{{{core_id}:"([a-f0-9]+)"', builder_js)
        if not hash_match:
            raise HTTPException(status_code=502, detail="Could not find rendering-core chunk hash")
        core_hash = hash_match.group(1)

        # 3. Find the worker URL from the rendering-core chunk
        core_url = f"{RESUMEIO_BASE}/assets/chunk/rendering-core.{core_hash}.js"
        core_js = self._get_public(core_url)
        worker_match = re.search(r"workers/rendering\.[a-f0-9]+\.js", core_js)
        if not worker_match:
            raise HTTPException(status_code=502, detail="Could not find rendering worker URL")
        worker_filename = worker_match.group()

        # 4. Download the main rendering worker
        worker_url = f"{RESUMEIO_BASE}/assets/{worker_filename}"
        self._download(worker_url, WORKER_CACHE_DIR / "rendering.js")
        worker_code = (WORKER_CACHE_DIR / "rendering.js").read_text()

        # 5. Download numbered chunks (e.g., workers/171.xxx.js)
        for chunk_ref in re.findall(r"workers/\d+\.[a-f0-9]+\.js", worker_code):
            filename = chunk_ref.split("/")[-1]
            self._download(f"{RESUMEIO_BASE}/assets/{chunk_ref}", WORKER_CACHE_DIR / filename)

        # 6. Download vendor chunks (e.g., workers/vendors.xxx.js)
        vendor_hashes = re.findall(r'\{[\d:,"a-f]+\}', worker_code)
        for block in vendor_hashes:
            for match in re.finditer(r'(\d+):"([a-f0-9]{16,})"', block):
                vendor_hash = match.group(2)
                filename = f"vendors.{vendor_hash}.js"
                if not (WORKER_CACHE_DIR / filename).exists():
                    try:
                        self._download(
                            f"{RESUMEIO_BASE}/assets/workers/{filename}",
                            WORKER_CACHE_DIR / filename,
                        )
                    except HTTPException:
                        pass  # Some hashes may not be vendor chunks

    def _get_public(self, url: str) -> str:
        """Fetch a public URL and return the text content."""
        response = requests.get(url, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Failed to fetch {url}")
        return response.text

    def _download(self, url: str, path: Path) -> None:
        """Download a file preserving raw bytes (some JS files embed binary data)."""
        logger.info("Downloading %s", path.name)
        response = requests.get(url, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Failed to fetch {url}")
        path.write_bytes(response.content)

    def _render(self, document: dict, config: dict) -> bytes:
        """Run the rendering Web Worker via Node.js and return the PDF bytes."""
        payload = json.dumps({
            "document": document,
            "config": config,
            "workerDir": str(WORKER_CACHE_DIR),
        })

        result = subprocess.run(
            ["node", str(RENDER_SCRIPT)],
            input=payload.encode(),
            capture_output=True,
            timeout=120,
        )

        if result.returncode != 0:
            error_msg = result.stderr.decode().strip()
            raise HTTPException(status_code=500, detail=f"Rendering failed: {error_msg}")

        return result.stdout
