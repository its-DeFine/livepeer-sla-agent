"""
CLI for the Livepeer SLA Agent.

Usage:
    sla-agent init          # Generate identity
    sla-agent status        # Show current capabilities
    sla-agent run           # Start the agent
    sla-agent attest        # Generate a one-off attestation
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from .identity import NodeIdentity, get_identity
from .capabilities import probe_capabilities
from .heartbeat import HeartbeatPublisher, LocalHeartbeatPublisher

app = typer.Typer(
    name="sla-agent",
    help="Livepeer SLA attestation agent"
)
console = Console()


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing identity"),
    key_path: Optional[Path] = typer.Option(None, "--key-path", "-k", help="Custom key path")
):
    """Initialize node identity (generate Ed25519 keypair)."""
    identity = NodeIdentity(key_path)

    try:
        node_id = identity.generate(force=force)
        console.print(Panel(
            f"[green]Identity generated successfully![/green]\n\n"
            f"[bold]Node ID:[/bold] {node_id}\n\n"
            f"[dim]Key stored at: {identity.key_path}[/dim]",
            title="🔑 New Identity"
        ))
    except FileExistsError:
        existing_id = identity.load()
        console.print(Panel(
            f"[yellow]Identity already exists![/yellow]\n\n"
            f"[bold]Node ID:[/bold] {existing_id}\n\n"
            f"[dim]Use --force to regenerate[/dim]",
            title="🔑 Existing Identity"
        ))


@app.command()
def status(
    key_path: Optional[Path] = typer.Option(None, "--key-path", "-k")
):
    """Show node identity and current capabilities."""
    try:
        identity = NodeIdentity(key_path)
        identity.load()
    except FileNotFoundError:
        console.print("[red]No identity found. Run 'sla-agent init' first.[/red]")
        raise typer.Exit(1)

    # Display identity
    console.print(Panel(
        f"[bold]Node ID:[/bold] {identity.node_id}",
        title="🔑 Identity"
    ))

    # Probe capabilities
    with console.status("Probing capabilities..."):
        caps = probe_capabilities()

    # CPU table
    cpu_table = Table(title="CPU")
    cpu_table.add_column("Property", style="cyan")
    cpu_table.add_column("Value", style="green")
    cpu_table.add_row("Model", caps.cpu.model)
    cpu_table.add_row("Cores (Physical)", str(caps.cpu.cores_physical))
    cpu_table.add_row("Cores (Logical)", str(caps.cpu.cores_logical))
    cpu_table.add_row("Architecture", caps.cpu.architecture)
    console.print(cpu_table)

    # Memory table
    mem_table = Table(title="Memory")
    mem_table.add_column("Property", style="cyan")
    mem_table.add_column("Value", style="green")
    mem_table.add_row("Total", f"{caps.memory.total_mb:,} MB")
    mem_table.add_row("Available", f"{caps.memory.available_mb:,} MB")
    mem_table.add_row("Swap", f"{caps.memory.swap_total_mb:,} MB")
    console.print(mem_table)

    # GPU table
    if caps.gpus:
        gpu_table = Table(title="GPUs")
        gpu_table.add_column("Index", style="cyan")
        gpu_table.add_column("Name", style="green")
        gpu_table.add_column("Memory", style="yellow")
        gpu_table.add_column("NVENC", style="magenta")
        for gpu in caps.gpus:
            gpu_table.add_row(
                str(gpu.index),
                gpu.name,
                f"{gpu.memory_total_mb:,} MB",
                "✓" if gpu.nvenc_supported else "✗"
            )
        console.print(gpu_table)
    else:
        console.print("[yellow]No GPUs detected[/yellow]")

    # Network
    net_table = Table(title="Network")
    net_table.add_column("Property", style="cyan")
    net_table.add_column("Value", style="green")
    net_table.add_row("Hostname", caps.network.hostname)
    net_table.add_row("Primary IP", caps.network.primary_ip)
    console.print(net_table)

    # Livepeer
    lp_table = Table(title="Livepeer")
    lp_table.add_column("Property", style="cyan")
    lp_table.add_column("Value", style="green")
    lp_table.add_row("Transcoder", "✓ Running" if caps.livepeer.transcoder_available else "✗ Not detected")
    if caps.livepeer.orchestrator_address:
        lp_table.add_row("Orchestrator", caps.livepeer.orchestrator_address)
    lp_table.add_row("Codecs", ", ".join(caps.livepeer.supported_codecs) or "N/A")
    console.print(lp_table)


@app.command()
def attest(
    key_path: Optional[Path] = typer.Option(None, "--key-path", "-k"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to file"),
    pretty: bool = typer.Option(True, "--pretty/--compact", help="Pretty print JSON")
):
    """Generate a signed capability attestation."""
    try:
        identity = get_identity(key_path)
    except FileNotFoundError:
        console.print("[red]No identity found. Run 'sla-agent init' first.[/red]")
        raise typer.Exit(1)

    caps = probe_capabilities()
    attestation = identity.sign_attestation({
        "type": "capability_snapshot",
        "capabilities": caps.to_dict()
    })

    json_str = attestation.to_json() if pretty else json.dumps({
        "node_id": attestation.node_id,
        "timestamp": attestation.timestamp,
        "payload": attestation.payload,
        "signature": attestation.signature
    })

    if output:
        output.write_text(json_str)
        console.print(f"[green]Attestation written to {output}[/green]")
    else:
        console.print(json_str)


@app.command()
def verify(
    attestation_file: Path = typer.Argument(..., help="Attestation JSON file to verify")
):
    """Verify a signed attestation."""
    from .identity import SignedAttestation, NodeIdentity

    try:
        data = json.loads(attestation_file.read_text())
        attestation = SignedAttestation(**data)
    except Exception as e:
        console.print(f"[red]Failed to parse attestation: {e}[/red]")
        raise typer.Exit(1)

    is_valid = NodeIdentity.verify_attestation(attestation)

    if is_valid:
        console.print(Panel(
            f"[green]✓ Signature is valid![/green]\n\n"
            f"[bold]Node ID:[/bold] {attestation.node_id}\n"
            f"[bold]Timestamp:[/bold] {attestation.timestamp}\n"
            f"[bold]Type:[/bold] {attestation.payload.get('type', 'unknown')}",
            title="Verification Result"
        ))
    else:
        console.print(Panel(
            "[red]✗ Signature is INVALID![/red]\n\n"
            "This attestation may have been tampered with.",
            title="Verification Result"
        ))
        raise typer.Exit(1)


@app.command()
def run(
    dashboard_url: str = typer.Option(
        "http://localhost:8080",
        "--dashboard", "-d",
        help="Dashboard URL to send heartbeats"
    ),
    port: int = typer.Option(9090, "--port", "-p", help="Agent server port"),
    interval: int = typer.Option(60, "--interval", "-i", help="Heartbeat interval (seconds)"),
    key_path: Optional[Path] = typer.Option(None, "--key-path", "-k"),
    local: bool = typer.Option(False, "--local", "-l", help="Store attestations locally instead of sending")
):
    """Start the SLA agent (server + heartbeat publisher)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    try:
        identity = get_identity(key_path)
    except FileNotFoundError:
        console.print("[red]No identity found. Run 'sla-agent init' first.[/red]")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Node ID:[/bold] {identity.node_id}\n"
        f"[bold]Dashboard:[/bold] {dashboard_url}\n"
        f"[bold]Agent Port:[/bold] {port}\n"
        f"[bold]Heartbeat Interval:[/bold] {interval}s",
        title="🚀 Starting SLA Agent"
    ))

    from .server import run_server
    run_server(
        host="0.0.0.0",
        port=port,
        dashboard_url=dashboard_url,
        heartbeat_interval=interval
    )


if __name__ == "__main__":
    app()
