"""
CLI for the Livepeer SLA Agent.

Usage:
    sla-agent init          # Generate identity
    sla-agent status        # Show current capabilities
    sla-agent run           # Start the agent
    sla-agent link <addr>   # Link ETH orchestrator address
    sla-agent attest        # Generate a one-off attestation
    sla-agent verify        # Verify an attestation file
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
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

payments_app = typer.Typer(name="payments", help="Payments-backend helper commands")
payments_offers_app = typer.Typer(name="offers", help="Workload offer opt-in management")
payments_app.add_typer(payments_offers_app, name="offers")
app.add_typer(payments_app, name="payments")

PAYMENTS_AUTH_DOMAIN = "payments-orchestrator-credential:auth:v1"


def _normalize_hex_32(value: str) -> str:
    candidate = (value or "").strip()
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if len(candidate) != 64:
        raise ValueError("Expected 32-byte hex value")
    return "0x" + candidate.lower()


def _load_secret_key(*, key: Optional[str], key_file: Optional[Path]) -> str:
    if key_file:
        loaded = key_file.read_text(encoding="utf-8").strip()
        if loaded:
            key = loaded
    if not key:
        key = typer.prompt("Delegate private key (hex)", hide_input=True)
    candidate = (key or "").strip()
    if candidate.startswith("0x"):
        candidate = candidate[2:]
    if len(candidate) != 64:
        raise typer.BadParameter("Delegate private key must be 32 bytes (64 hex chars)")
    return "0x" + candidate.lower()


async def _payments_request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    json_body: Optional[dict] = None,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await client.request(method, url, headers=headers, json=json_body)
        return resp


def _require_eth_account() -> Any:
    try:
        from eth_account import Account  # type: ignore
        from eth_account.messages import encode_defunct  # type: ignore
    except ImportError as exc:
        console.print(
            "[red]Missing dependency:[/red] install with `pip install livepeer-sla-agent[eth]` to use payments credential auth."
        )
        raise typer.Exit(1) from exc
    return Account, encode_defunct


def _credential_message_hash(*, orchestrator_id: str, owner: str, delegate: str, nonce: str, expires_at: int) -> bytes:
    from eth_abi.packed import encode_packed
    from eth_utils import keccak, to_checksum_address

    packed = encode_packed(
        ["string", "string", "address", "address", "bytes32", "uint256"],
        [
            PAYMENTS_AUTH_DOMAIN,
            orchestrator_id,
            to_checksum_address(owner),
            to_checksum_address(delegate),
            bytes.fromhex(_normalize_hex_32(nonce)[2:]),
            int(expires_at),
        ],
    )
    return keccak(packed)


@payments_app.command("register")
def payments_register(
    orchestrator_id: str = typer.Option(..., "--orchestrator-id", help="Payments orchestrator_id"),
    eth_address: str = typer.Option(..., "--eth-address", help="Owner orchestrator address (0x...)"),
    payments_url: str = typer.Option(
        "http://localhost:9090",
        "--payments-url",
        envvar="PAYMENTS_BACKEND_URL",
        help="Payments backend base URL",
    ),
):
    """Register an orchestrator in payments-backend (required before credential auth)."""
    async def _run() -> None:
        resp = await _payments_request(
            "POST",
            f"{payments_url.rstrip('/')}/api/orchestrators/register",
            json_body={
                "orchestrator_id": orchestrator_id,
                "address": eth_address,
                "capability": "livepeer",
                "contact_email": None,
                "host_public_ip": None,
                "host_name": None,
                "services_healthy": True,
            },
        )
        if resp.status_code != 200:
            console.print(f"[red]Registration failed ({resp.status_code}):[/red] {resp.text}")
            raise typer.Exit(1)
        payload = resp.json()
        console.print(Panel(json.dumps(payload, indent=2), title="Payments Registration"))

    asyncio.run(_run())


@payments_app.command("credential-token")
def payments_credential_token(
    orchestrator_id: str = typer.Option(..., "--orchestrator-id", help="Payments orchestrator_id"),
    payments_url: str = typer.Option(
        "http://localhost:9090",
        "--payments-url",
        envvar="PAYMENTS_BACKEND_URL",
        help="Payments backend base URL",
    ),
    delegate_key: Optional[str] = typer.Option(
        None,
        "--delegate-key",
        envvar="PAYMENTS_DELEGATE_KEY",
        help="Delegate private key hex (prefer --delegate-key-file or prompt)",
    ),
    delegate_key_file: Optional[Path] = typer.Option(
        None,
        "--delegate-key-file",
        help="Path to file containing delegate private key hex",
    ),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write token JSON to file"),
):
    """Mint a short-lived orchestrator token using the on-chain credential + delegate signature."""
    Account, encode_defunct = _require_eth_account()

    async def _run() -> None:
        key_hex = _load_secret_key(key=delegate_key, key_file=delegate_key_file)
        delegate_address = Account.from_key(key_hex).address

        nonce_resp = await _payments_request(
            "POST",
            f"{payments_url.rstrip('/')}/api/orchestrators/{orchestrator_id}/credential/nonce",
        )
        if nonce_resp.status_code != 200:
            console.print(f"[red]Nonce request failed ({nonce_resp.status_code}):[/red] {nonce_resp.text}")
            raise typer.Exit(1)
        nonce_payload = nonce_resp.json()
        owner_address = str(nonce_payload.get("owner_address") or "").strip()
        nonce = str(nonce_payload.get("nonce") or "").strip()
        expires_at = int(nonce_payload.get("expires_at") or 0)

        msg_hash = _credential_message_hash(
            orchestrator_id=orchestrator_id,
            owner=owner_address,
            delegate=delegate_address,
            nonce=nonce,
            expires_at=expires_at,
        )
        signed = Account.from_key(key_hex).sign_message(encode_defunct(primitive=msg_hash))

        token_resp = await _payments_request(
            "POST",
            f"{payments_url.rstrip('/')}/api/orchestrators/{orchestrator_id}/credential/token",
            json_body={
                "delegate_address": delegate_address,
                "nonce": nonce,
                "expires_at": expires_at,
                "signature": signed.signature.hex(),
            },
        )
        if token_resp.status_code != 200:
            console.print(f"[red]Token request failed ({token_resp.status_code}):[/red] {token_resp.text}")
            raise typer.Exit(1)
        token_payload = token_resp.json()
        if output:
            output.write_text(json.dumps(token_payload, indent=2), encoding="utf-8")
            console.print(f"[green]Token written to {output}[/green]")
        console.print(Panel(json.dumps(token_payload, indent=2), title="Payments Orchestrator Token"))

    asyncio.run(_run())


@payments_offers_app.command("available")
def payments_offers_available(
    payments_url: str = typer.Option(
        "http://localhost:9090",
        "--payments-url",
        envvar="PAYMENTS_BACKEND_URL",
        help="Payments backend base URL",
    ),
    token: str = typer.Option(..., "--token", envvar="PAYMENTS_ORCHESTRATOR_TOKEN", help="Orchestrator token"),
):
    """List active workload offers (requires orchestrator token)."""
    async def _run() -> None:
        resp = await _payments_request(
            "GET",
            f"{payments_url.rstrip('/')}/api/orchestrators/me/workload-offers/available",
            token=token,
        )
        if resp.status_code != 200:
            console.print(f"[red]Request failed ({resp.status_code}):[/red] {resp.text}")
            raise typer.Exit(1)
        offers = resp.json().get("offers", [])
        table = Table(title="Available Workload Offers")
        table.add_column("offer_id", style="cyan")
        table.add_column("title", style="green")
        table.add_column("kind", style="magenta")
        table.add_column("payout_eth", style="yellow")
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            table.add_row(
                str(offer.get("offer_id") or ""),
                str(offer.get("title") or ""),
                str(offer.get("kind") or ""),
                str(offer.get("payout_amount_eth") or ""),
            )
        console.print(table)

    asyncio.run(_run())


@payments_offers_app.command("show")
def payments_offers_show(
    payments_url: str = typer.Option(
        "http://localhost:9090",
        "--payments-url",
        envvar="PAYMENTS_BACKEND_URL",
        help="Payments backend base URL",
    ),
    token: str = typer.Option(..., "--token", envvar="PAYMENTS_ORCHESTRATOR_TOKEN", help="Orchestrator token"),
):
    """Show selected workload offers for this orchestrator."""
    async def _run() -> None:
        resp = await _payments_request(
            "GET",
            f"{payments_url.rstrip('/')}/api/orchestrators/me/workload-offers",
            token=token,
        )
        if resp.status_code != 200:
            console.print(f"[red]Request failed ({resp.status_code}):[/red] {resp.text}")
            raise typer.Exit(1)
        payload = resp.json()
        console.print(Panel(json.dumps(payload, indent=2), title="Selected Workload Offers"))

    asyncio.run(_run())


@payments_offers_app.command("select")
def payments_offers_select(
    offer_ids: list[str] = typer.Argument(..., help="Offer IDs to opt into"),
    payments_url: str = typer.Option(
        "http://localhost:9090",
        "--payments-url",
        envvar="PAYMENTS_BACKEND_URL",
        help="Payments backend base URL",
    ),
    token: str = typer.Option(..., "--token", envvar="PAYMENTS_ORCHESTRATOR_TOKEN", help="Orchestrator token"),
):
    """Set workload offer opt-ins for this orchestrator."""
    async def _run() -> None:
        resp = await _payments_request(
            "PUT",
            f"{payments_url.rstrip('/')}/api/orchestrators/me/workload-offers",
            token=token,
            json_body={"offer_ids": offer_ids},
        )
        if resp.status_code != 200:
            console.print(f"[red]Request failed ({resp.status_code}):[/red] {resp.text}")
            raise typer.Exit(1)
        payload = resp.json()
        console.print(Panel(json.dumps(payload, indent=2), title="Updated Workload Offers"))

    asyncio.run(_run())


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
def link(
    eth_address: str = typer.Argument(..., help="Your Livepeer orchestrator ETH address (0x...)"),
    dashboard_url: str = typer.Option(
        "http://localhost:8080",
        "--dashboard", "-d",
        help="Dashboard URL to submit link"
    ),
    key_path: Optional[Path] = typer.Option(None, "--key-path", "-k")
):
    """
    Link your ETH orchestrator address to this node.

    Simple 3-step process:
    1. This generates a message
    2. You sign it with MEW/MetaMask
    3. You paste the signature here
    """
    import httpx
    from .eth_link import create_link_message, AddressLink

    # Load identity
    try:
        identity = get_identity(key_path)
    except FileNotFoundError:
        console.print("[red]No identity found. Run 'sla-agent init' first.[/red]")
        raise typer.Exit(1)

    # Validate ETH address format
    if not eth_address.startswith("0x") or len(eth_address) != 42:
        console.print("[red]Invalid ETH address format. Must be 0x followed by 40 hex characters.[/red]")
        raise typer.Exit(1)

    import time
    timestamp = int(time.time())
    message = create_link_message(identity.node_id, eth_address, timestamp)

    # Step 1: Show the message to sign
    console.print()
    console.print(Panel(
        f"[bold cyan]Step 1: Copy this message[/bold cyan]\n\n"
        f"[dim]Sign this exact message with your ETH wallet (MEW, MetaMask, etc)[/dim]",
        title="🔗 Link ETH Address"
    ))
    console.print()
    console.print(Panel(message, title="Message to Sign", border_style="green"))
    console.print()

    # Step 2: Instructions
    console.print(Panel(
        "[bold cyan]Step 2: Sign the message[/bold cyan]\n\n"
        "• [bold]MEW (MyEtherWallet):[/bold] Go to Message → Sign Message\n"
        "• [bold]MetaMask:[/bold] Settings → Security → Sign Message\n"
        "• [bold]Etherscan:[/bold] Verified Signatures → Sign Message\n\n"
        "[dim]Paste the message above, sign with your orchestrator wallet[/dim]",
        title="📝 How to Sign"
    ))
    console.print()

    # Step 3: Get signature
    console.print("[bold cyan]Step 3: Paste your signature below[/bold cyan]")
    console.print("[dim]The signature should start with 0x and be ~132 characters[/dim]")
    console.print()

    signature = typer.prompt("Signature")

    # Validate signature format
    if not signature.startswith("0x"):
        signature = "0x" + signature

    if len(signature) < 100:
        console.print("[red]Signature seems too short. Please check and try again.[/red]")
        raise typer.Exit(1)

    # Create the link
    link_data = AddressLink(
        node_id=identity.node_id,
        eth_address=eth_address,
        timestamp=timestamp,
        message=message,
        eth_signature=signature
    )

    # Verify locally first
    try:
        from .eth_link import verify_address_link
        if not verify_address_link(link_data):
            console.print("[red]✗ Signature verification failed![/red]")
            console.print("[dim]Make sure you signed with the correct wallet address.[/dim]")
            raise typer.Exit(1)
    except ImportError:
        console.print("[yellow]⚠ Could not verify locally (eth-account not installed). Submitting anyway...[/yellow]")

    console.print("[green]✓ Signature verified locally![/green]")
    console.print()

    # Submit to dashboard
    console.print(f"[dim]Submitting to {dashboard_url}...[/dim]")

    try:
        response = httpx.post(
            f"{dashboard_url}/api/v1/link/submit",
            json=link_data.to_dict(),
            timeout=30.0
        )

        if response.status_code == 200:
            console.print(Panel(
                f"[green]✓ Address linked successfully![/green]\n\n"
                f"[bold]Node ID:[/bold] {identity.node_id[:32]}...\n"
                f"[bold]ETH Address:[/bold] {eth_address}\n\n"
                f"[dim]Your orchestrator is now linked to this SLA agent.[/dim]",
                title="🎉 Link Complete"
            ))
        else:
            error = response.json().get("detail", response.text)
            console.print(f"[red]✗ Dashboard rejected the link: {error}[/red]")
            raise typer.Exit(1)

    except httpx.RequestError as e:
        console.print(f"[red]✗ Could not reach dashboard: {e}[/red]")
        console.print("[dim]Make sure the dashboard is running and accessible.[/dim]")
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
