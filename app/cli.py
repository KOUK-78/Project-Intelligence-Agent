"""CLI only. No logic here: call run_agent, print the result."""
import typer

from app.runtime import get_run_agent

app = typer.Typer(add_completion=False, help="Project Intelligence Agent")


@app.callback()
def _main() -> None:
    """Project Intelligence Agent."""


@app.command()
def ask(
    question: str = typer.Argument(..., help="Your question, in quotes"),
    stub: bool = typer.Option(False, "--stub", help="Use the stub agent"),
) -> None:
    """Ask a project question and print the cited answer."""
    r = get_run_agent(use_stub=stub)(question)
    typer.echo(f"Answer:     {r['answer']}")
    typer.echo(f"Citations:  {', '.join(r['citations']) or '-'}")
    typer.echo(f"Status:     {r['status']}")
    typer.echo(f"Trace ID:   {r['trace_id'] or '-'}")
    typer.echo(f"Latency:    {r['latency_ms']} ms")
    typer.echo("Tool calls:")
    for c in r["tool_calls"] or []:
        typer.echo(f"  - {c['name']}({c['args']})  {c['latency_ms']} ms")
    if not r["tool_calls"]:
        typer.echo("  (none)")
