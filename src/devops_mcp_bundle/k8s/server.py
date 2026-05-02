"""FastMCP entry point for the Kubernetes Inspector server.

Read-only by design: every tool calls `list_*` or `read_*`. There are no
helpers for `delete`, `patch`, `apply`, or `exec`, and the safety section
of the README spells that out.

Authentication uses the standard kubernetes_asyncio loaders: in-cluster
config when running as a pod, otherwise `~/.kube/config` (override with
`KUBECONFIG`).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import typer
from fastmcp import FastMCP

from devops_mcp_bundle.k8s import queries
from devops_mcp_bundle.k8s.models import (
    ConfigMapInfo,
    Event,
    LogLine,
    Namespace,
    OOMKill,
    Pod,
    PodMetric,
    PodSpec,
    ResourceQuotaInfo,
)

mcp: FastMCP = FastMCP(
    name="k8s-inspector",
    instructions=(
        "Read-only Kubernetes inspector. Use list_namespaces / list_pods / "
        "describe_pod for shape; pod_logs / pod_events / recent_oomkills "
        "for incident triage; top_pods for live CPU/memory."
    ),
)


@asynccontextmanager
async def _api() -> AsyncIterator[tuple[object, object]]:
    """Yield (CoreV1Api, CustomObjectsApi) configured from env or kubeconfig."""
    from kubernetes_asyncio import client, config  # noqa: PLC0415

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()  # type: ignore[no-untyped-call]
    else:
        await config.load_kube_config()
    api_client = client.ApiClient()
    try:
        yield client.CoreV1Api(api_client), client.CustomObjectsApi(api_client)
    finally:
        await api_client.close()


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


@mcp.tool
async def list_namespaces() -> list[Namespace]:
    """List every namespace and its phase + age."""
    async with _api() as (core, _):
        return await queries.list_namespaces(core)  # type: ignore[arg-type]


@mcp.tool
async def list_pods(namespace: str, label_selector: str | None = None) -> list[Pod]:
    """List pods in `namespace`. Optional `label_selector` ('app=foo,env=prod')."""
    async with _api() as (core, _):
        return await queries.list_pods(core, namespace, label_selector)  # type: ignore[arg-type]


@mcp.tool
async def describe_pod(namespace: str, name: str) -> PodSpec:
    """Container images, resources, conditions, labels for one pod."""
    async with _api() as (core, _):
        return await queries.describe_pod(core, namespace, name)  # type: ignore[arg-type]


@mcp.tool
async def pod_logs(
    namespace: str, name: str, container: str | None = None, tail: int = 200
) -> list[LogLine]:
    """Tail the last `tail` lines of `container` in pod `namespace/name`.

    `tail` is capped at :data:`queries.MAX_POD_LOG_TAIL`. For deeper
    history, route the agent through the observability server (Loki).
    """
    # Validate before opening a kube client — saves a TCP roundtrip on
    # the obvious-mistake path. Mirrors the same check inside
    # `queries.pod_logs` so the error message is identical no matter
    # which entry point the caller hits.
    if tail <= 0:
        raise ValueError("tail must be positive")
    if tail > queries.MAX_POD_LOG_TAIL:
        raise ValueError(f"tail must be <= {queries.MAX_POD_LOG_TAIL}")
    async with _api() as (core, _):
        return await queries.pod_logs(core, namespace, name, container, tail)  # type: ignore[arg-type]


@mcp.tool
async def pod_events(namespace: str, name: str) -> list[Event]:
    """All events whose `involvedObject.name == name` in `namespace`."""
    async with _api() as (core, _):
        return await queries.pod_events(core, namespace, name)  # type: ignore[arg-type]


@mcp.tool
async def top_pods(namespace: str) -> list[PodMetric]:
    """Live CPU (millicores) + memory (bytes) per pod (needs metrics-server)."""
    async with _api() as (_, custom):
        return await queries.top_pods(custom, namespace)  # type: ignore[arg-type]


@mcp.tool
async def recent_oomkills(namespace: str, since_min: int = 60) -> list[OOMKill]:
    """OOM-related Warning events from the last `since_min` minutes."""
    async with _api() as (core, _):
        return await queries.recent_oomkills(core, namespace, since_min)  # type: ignore[arg-type]


@mcp.tool
async def list_configmaps(namespace: str) -> list[ConfigMapInfo]:
    """List ConfigMaps in `namespace` (key names only — no values)."""
    async with _api() as (core, _):
        return await queries.list_configmaps(core, namespace)  # type: ignore[arg-type]


@mcp.tool
async def namespace_events(
    namespace: str, only_warnings: bool = True, since_min: int | None = None
) -> list[Event]:
    """Cluster events for `namespace` (Warning-only by default)."""
    async with _api() as (core, _):
        return await queries.namespace_events(
            core,  # type: ignore[arg-type]
            namespace,
            only_warnings=only_warnings,
            since_min=since_min,
        )


@mcp.tool
async def resource_quotas(namespace: str) -> list[ResourceQuotaInfo]:
    """ResourceQuotas in `namespace` with computed per-resource headroom."""
    async with _api() as (core, _):
        return await queries.resource_quotas(core, namespace)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


_cli = typer.Typer(name="mcp-k8s-inspector", add_completion=False)


@_cli.command()
def run(
    transport: str = typer.Option("stdio", help="MCP transport: stdio | http"),
    host: str = typer.Option("127.0.0.1", help="HTTP bind host."),
    port: int = typer.Option(8081, help="HTTP bind port."),
) -> None:
    """Run the Kubernetes Inspector MCP server."""
    if transport == "stdio":
        asyncio.run(mcp.run_stdio_async())
    elif transport == "http":
        asyncio.run(mcp.run_http_async(host=host, port=port))
    else:
        raise typer.BadParameter(f"unknown transport {transport!r}")


def main() -> None:  # pragma: no cover - thin wrapper
    _cli()


if __name__ == "__main__":  # pragma: no cover
    main()
