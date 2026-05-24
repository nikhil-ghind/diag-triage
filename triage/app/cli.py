"""Operator CLI (`diag-triage ...`)."""
from __future__ import annotations

import asyncio

import typer
import uvicorn

from triage.config import get_settings

app = typer.Typer(help="diag-triage — hardware diagnostic log triage service")


@app.command()
def serve(host: str = "", port: int = 0):
    """Run the FastAPI ingestion/triage service."""
    s = get_settings()
    uvicorn.run("triage.app.main:app", host=host or s.host, port=port or s.port,
                log_level=s.log_level)


@app.command("mcp-server")
def mcp_server():
    """Run the MCP server exposing diagnostic query tools over stdio."""
    from triage.mcp.server import main as mcp_main
    mcp_main()


@app.command("init-indices")
def init_indices():
    """Create Elasticsearch indices + ILM policy."""
    from triage.ingest.es_client import ESClient

    async def _run():
        es = ESClient(get_settings())
        await es.ensure_indices()
        await es.close()
        typer.echo("indices ready")

    asyncio.run(_run())


@app.command()
def replay(path: str, host: str = "replay-host"):
    """Replay a newline-delimited log file through the pipeline."""
    from triage.app.service import TriageService

    async def _run():
        svc = TriageService(get_settings())
        await svc.startup()
        with open(path) as f:
            lines = [ln.rstrip("\n") for ln in f]
        result = await svc.ingest(host, lines)
        typer.echo(result)
        await svc.shutdown()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
