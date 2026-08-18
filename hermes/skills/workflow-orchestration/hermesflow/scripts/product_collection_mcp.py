"""Narrow MCP transport for invoking the approved product-collection flow with args."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

FLOW_PATH = "f/workflows/product_collection"
FLOW_VERSION = "1.0.0"
WINDMILL_URL = "http://windmill_server:8000"
WORKSPACE = "main"
ENV_FILE = Path("/opt/data/.env")

mcp = FastMCP("hermesflow-product-collection")


class ProductSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=1)
    allowed_domains: list[str] = Field(..., min_length=1, max_length=20)
    source_type: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("allowed_domains")
    @classmethod
    def _domains_are_normalized(cls, value: list[str]) -> list[str]:
        normalized = [domain.rstrip(".").lower() for domain in value]
        if any(not domain or ":" in domain or "/" in domain for domain in normalized):
            raise ValueError("allowed_domains must contain hostnames only")
        return normalized

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source URL must be absolute HTTP(S)")
        return value


class ProductCollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[ProductSource] = Field(..., min_length=1, max_length=20)
    max_concurrency: int = Field(default=4, ge=1, le=8)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_size_bytes: int = Field(default=5_000_000, ge=1, le=100_000_000)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_products_per_source: int = Field(default=100, ge=1, le=1000)

    @field_validator("sources")
    @classmethod
    def _sources_are_unique_and_narrow(cls, value: list[ProductSource]) -> list[ProductSource]:
        ids = [source.source_id for source in value]
        if len(ids) != len(set(ids)):
            raise ValueError("source_id values must be unique")
        for source in value:
            hostname = (urlsplit(source.url).hostname or "").rstrip(".").lower()
            if hostname not in source.allowed_domains:
                raise ValueError(
                    f"source {source.source_id!r} must explicitly allow its URL hostname {hostname!r}"
                )
        return value


def _windmill_token() -> str:
    token = (
        os.environ.get("WINDMILL_MCP_TOKEN", "")
        or os.environ.get("WM_MCP_TOKEN", "")
    ).strip()
    if token:
        return token
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("WINDMILL_MCP_TOKEN="):
                token = line.split("=", 1)[1].strip()
                if token:
                    return token
    raise RuntimeError("WINDMILL_MCP_TOKEN is not configured in the Hermes environment")


def flow_arguments(request: ProductCollectionRequest) -> dict:
    """Build the only argument shape this transport is permitted to submit."""
    return {
        "sources": [source.model_dump(mode="json") for source in request.sources],
        "db": "$res:f/collection/collection_db",
        "enable_ai_fallback": False,
        "max_concurrency": request.max_concurrency,
        "timeout_seconds": request.timeout_seconds,
        "max_size_bytes": request.max_size_bytes,
        "max_retries": request.max_retries,
        "max_products_per_source": request.max_products_per_source,
    }


async def submit_product_collection(request: ProductCollectionRequest) -> dict:
    endpoint = f"{WINDMILL_URL}/api/w/{WORKSPACE}/jobs/run/f/{FLOW_PATH}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {_windmill_token()}"},
            json=flow_arguments(request),
        )
        response.raise_for_status()
    job_id = response.text.strip().strip('"')
    if not job_id:
        raise RuntimeError("Windmill accepted the request without returning a job ID")
    return {
        "job_id": job_id,
        "workspace": WORKSPACE,
        "workflow_path": FLOW_PATH,
        "workflow_version": FLOW_VERSION,
    }


@mcp.tool(
    description=(
        "Run the approved read-only-intent product collection workflow with explicit, "
        "bounded source arguments. This tool cannot select another flow, enable AI "
        "fallback, schedule work, or mutate a source system."
    )
)
async def run_product_collection(
    sources: list[ProductSource],
    max_concurrency: int = 4,
    timeout_seconds: float = 30,
    max_size_bytes: int = 5_000_000,
    max_retries: int = 2,
    max_products_per_source: int = 100,
) -> dict:
    request = ProductCollectionRequest(
        sources=sources,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        max_size_bytes=max_size_bytes,
        max_retries=max_retries,
        max_products_per_source=max_products_per_source,
    )
    return await submit_product_collection(request)


if __name__ == "__main__":
    mcp.run(transport="stdio")
