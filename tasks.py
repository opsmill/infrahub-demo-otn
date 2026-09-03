"""Every command this demo needs, as an invoke task.

Run `uv run invoke list` for the table, or `uv run invoke --list` for the same
information without the formatting.

Nothing here should require you to remember an `infrahubctl` invocation or to
export a token first. If a task makes you do either, that is a defect in this
file.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess  # noqa: S404
import sys
import time
from typing import Any, NamedTuple

import httpx
from invoke import Collection, Context, task
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

REPO_ROOT = pathlib.Path(__file__).parent.resolve()
ENV_FILE = REPO_ROOT / ".env"
DOCS_DIRECTORY = REPO_ROOT / "docs"

BASE_VERSION = os.getenv("INFRAHUB_BASE_VERSION", "1.11.0")
IMAGE = f"opsmill/infrahub-demo-otn:{BASE_VERSION}"
PROJECT = os.getenv("INFRAHUB_DEMO_PROJECT", "infrahub-demo-otn")
"""The Compose project the lifecycle tasks build their commands from.

Overridable, with the three port variables `docker-compose.override.yml` reads,
so a test stack cannot collide with a developer's. `destroy` runs `down -v`.
"""

VENDOR_SOURCE = "opsmill/schema-library base/location.yml"
VENDOR_REF = "v1.4.11"
VENDOR_URL = f"https://raw.githubusercontent.com/opsmill/schema-library/{VENDOR_REF}/base/location.yml"
"""The upstream `schemas/location.yml` was copied from, and the ref it names in
its own header. Both move together or the header is a lie."""

COMPOSE = (
    f"curl -sL https://infrahub.opsmill.io/{BASE_VERSION} | "
    f"docker compose -f - -f docker-compose.override.yml -p {PROJECT}"
)

REMOTE_DIRECTORY = REPO_ROOT / ".remote"
"""Where `load-repository` writes the export the containers clone from.

Bind-mounted to `/remote` by `docker-compose.override.yml`. Gitignored: it is
a build product, and committing a copy of the repository into the repository
is a loop.
"""

REMOTE_MOUNT = "/remote"
"""The same directory as the containers see it.

A `CoreRepository.location` is resolved inside the container, so a host path
is a path the server cannot reach. This repository has no git remote and has
never been pushed, so a URL is not an alternative here.
"""

REPOSITORY_NAME = "infrahub-demo-otn"
"""The name of the `CoreRepository` object. One per stack.

Spelled out rather than read from `PROJECT`, which is now overridable: moving a
stack to its own Compose project must not rename an object inside the graph.
"""

# Every entry under `check_definitions` in .infrahub.yml. Listed rather than
# globbed over checks/: the registration is what makes a file a check, and an
# unregistered .py in that directory is not one.
#
# This list was two names behind: `container_capacity` arrived with feature 016
# and `diversity` with 017, and neither reached here, so `invoke check` ran three
# of the five and `invoke check --name diversity` refused a check that exists.
# Nothing failed loudly, which is the failure mode of a hand-kept mirror.
CHECKS = (
    "units_import",
    "osnr_margin",
    "channel_collision",
    "container_capacity",
    "diversity",
    "provisionable",
    "channel_count_consistency",
    "monitor_completeness",
    "carrier_termination",
)

DEMO_BRANCH = "demo"
RAMAN_BRANCH = "raman-par-mad"
"""The branch `demo-raman` puts the Paris to Madrid pumps on.

Its own branch rather than `demo`, because the scenario is a before and after:
the default branch has to keep the failing check for the comparison to mean
anything."""
DEMO_SERVICES = (
    "svc-ber-ams-400g",
    "svc-fra-mil-ai-400g",
    "svc-ams-mil-ai-400g",
    "svc-fra-gva-hpc-400g",
    "svc-vie-mil-hpc-400g",
)

WALKTHROUGH = (
    "demo-capacity",
    "demo-reach",
    "demo-provision",
    "demo-provision-all",
    "demo-trace",
    "demo-impact",
    "demo-srlg",
    "demo-latency",
    "demo-infiniband",
    "demo-refusal",
)
"""The runbook order in `docs/docs/loadable-scenarios.mdx`, and what the
`demo` task runs. `demo-setup` comes before it and `demo-clean` after it."""


class ScenarioBranch(NamedTuple):
    """A scenario's branch, the `demo/` files it loads and the check it runs."""

    task: str
    branch: str
    files: tuple[str, ...]
    check: str | None = None


# The single source for what a scenario owns. Each scenario reads its own row
# and `demo-clean` iterates the whole tuple, so a branch cannot be created by
# one list and cleaned up by another that fell behind it.
#
# The ten walkthrough steps share DEMO_BRANCH and appear once, under the task
# that loads their files. A loadable scenario owns a branch of its own, because
# it is a before and after and the default branch has to keep the "before".
SCENARIO_BRANCHES: tuple[ScenarioBranch, ...] = (
    ScenarioBranch(
        task="demo-setup",
        branch=DEMO_BRANCH,
        files=("demo/00_services.yml", "demo/01_impact_services.yml"),
    ),
    ScenarioBranch(
        task="demo-raman",
        branch=RAMAN_BRANCH,
        files=("demo/02_par_mad_raman.yml",),
        check="osnr_margin",
    ),
)


def _scenario(task_name: str) -> ScenarioBranch:
    """The `SCENARIO_BRANCHES` row one scenario task owns."""
    return next(row for row in SCENARIO_BRANCHES if row.task == task_name)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _load_env() -> dict[str, str]:
    """Read `.env` next to this file into a dict.

    `infrahubctl` resolves the address from `.env` but never the API token, so
    an unauthenticated read succeeds while the first write fails with
    "Authentication failure". Every task therefore passes both.

    The path is anchored to this file rather than to the working directory.
    `invoke` searches upwards for `tasks.py` and runs from wherever you called
    it, so a relative `.env` is not found from a subdirectory and the token is
    silently absent.
    """
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def _env() -> dict[str, str]:
    """`.env` with the process environment on top, so an exported variable wins.

    `.env` is a default and the environment is an instruction, which is the same
    precedence `tests/integration/conftest.py` applies through `setdefault`.
    """
    return {**_load_env(), **os.environ}


def _address() -> str:
    return _env().get("INFRAHUB_ADDRESS", "http://localhost:8000")


def _token() -> str:
    return _env().get("INFRAHUB_API_TOKEN", "")


def _fail(message: str, next_task: str | None = None) -> None:
    """Print a red line, name the task that fixes it, and stop."""
    console.print(f"[red]x[/red] {message}")
    if next_task:
        console.print(f"  Run [bold cyan]uv run invoke {next_task}[/bold cyan] first.")
    sys.exit(1)


def _run(context: Context, command: str, *, warn: bool = False) -> Any:
    """Run a command with the credentials in the environment.

    The token goes through `env=`, never into the command string: an argument
    is visible in `ps` to every user on the machine and copied into shell
    history along with the command.

    A missing `.env` is fatal only when the environment carries no address and
    token either. `.env` is gitignored, so a CI runner has none.
    """
    environment = _env()
    has_credentials = bool(environment.get("INFRAHUB_ADDRESS")) and bool(environment.get("INFRAHUB_API_TOKEN"))
    if not ENV_FILE.exists() and not has_credentials:
        _fail(
            f"No {ENV_FILE} file and no INFRAHUB_ADDRESS and INFRAHUB_API_TOKEN in the "
            "environment. Copy .env.example to .env before running this."
        )
    return context.run(command, pty=True, env=environment, warn=warn)


def _ctl(context: Context, arguments: str, *, warn: bool = False) -> Any:
    return _run(context, f"uv run infrahubctl {arguments}", warn=warn)


def _image_exists() -> bool:
    """Whether the custom image `invoke build` produces is present locally."""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", IMAGE],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _get(path: str, *, timeout: float = 5.0) -> Any | None:
    """One authenticated GET against the running stack. `None` when it is down."""
    try:
        response = httpx.get(
            f"{_address()}{path}",
            headers={"X-INFRAHUB-KEY": _token()},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def _count(kind: str, branch: str = "main") -> int | None:
    """Count objects of one kind on a branch. `None` when the stack is down."""
    try:
        response = httpx.post(
            f"{_address()}/graphql/{branch}",
            headers={"X-INFRAHUB-KEY": _token()},
            json={"query": f"{{ {kind} {{ count }} }}"},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    if payload.get("errors"):
        return None
    return int(payload["data"][kind]["count"])


def _graphql(document: str, branch: str = "main") -> Any:
    """One authenticated GraphQL call. Raises on transport and on `errors`."""
    response = httpx.post(
        f"{_address()}/graphql/{branch}",
        headers={"X-INFRAHUB-KEY": _token()},
        json={"query": document},
        timeout=60.0,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        _fail(f"GraphQL errors against {branch}: {payload['errors']}")
    return payload["data"]


MENU_NAMESPACE = "Otn"
"""The namespace `menus/otn.yml` writes, and the only one `load-menu` deletes."""


def _purge_menu(branch: str) -> int:
    """Delete every menu item this repository owns on one branch.

    `infrahubctl menu load` adds and updates and never deletes, and there is no
    delete subcommand. Without this, removing an entry from `menus/otn.yml`
    leaves it on the server: the sidebar becomes the union of the file and
    everything the file used to say, and a trim makes the sidebar longer.

    Scoped to the `Otn` namespace. An unscoped delete would take the core
    sidebar with it and the recovery from that is a rebuild.
    """
    data = _graphql(
        f'{{ CoreMenuItem(namespace__value: "{MENU_NAMESPACE}") {{ edges {{ node {{ id }} }} }} }}',
        branch,
    )
    ids = [edge["node"]["id"] for edge in data["CoreMenuItem"]["edges"]]
    for identifier in ids:
        _graphql(f'mutation {{ CoreMenuItemDelete(data: {{id: "{identifier}"}}) {{ ok }} }}', branch)
    return len(ids)


def _branch_exists(name: str) -> bool:
    """Whether a branch is on the server.

    Asked over GraphQL rather than over `/api/branch`, which is not a route on
    1.11 and answers 404. A REST reader turns that 404 into "no branches", so
    every caller silently believed every branch was missing.
    """
    try:
        response = httpx.post(
            f"{_address()}/graphql/main",
            headers={"X-INFRAHUB-KEY": _token()},
            json={"query": "{ Branch { name } }"},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return False
    if payload.get("errors"):
        return False
    return any(item.get("name") == name for item in payload["data"]["Branch"])


def _require_stack() -> None:
    """Stop with one sentence when the stack is not answering.

    Without this the first SDK call raises `URLNotFoundError` under a dozen
    frames of asyncio traceback, which tells a reader nothing about what to
    type next.
    """
    if _get("/api/schema/summary") is None:
        _fail(f"The stack at {_address()} is not answering.", "start")


def _ensure_branch(context: Context, name: str) -> None:
    """Create a branch, or continue onto the one that is already there."""
    if _branch_exists(name):
        console.print(f"[yellow]-[/yellow] branch {name} already exists, continuing onto it")
        return
    console.print(f"[yellow]-[/yellow] branch {name} does not exist yet, creating it in Git and in the graph")
    branch_create(context, name)


def _ensure_dataset(context: Context, branch: str) -> None:
    """Put the branch and the dataset in place before a scenario reads them.

    The stack check stays a refusal because a stack that is not answering is the
    one precondition a task cannot satisfy for itself. Everything after it is
    narrated: a task that silently spends a minute loading 2344 objects looks
    hung.
    """
    _require_stack()
    _ensure_branch(context, branch)
    # Counted after the branch exists, not before: a branch forked from a loaded
    # `main` inherits the dataset, and loading it again would cost a minute for
    # nothing.
    if _count("OtnSite", branch):
        console.print(f"[green]-[/green] the dataset is already there, branch {branch} is ready")
        return
    console.print(f"[yellow]-[/yellow] no dataset on {branch} yet, loading it")
    load(context, branch)


def _next_step(command: str) -> None:
    """Name the next command of the walkthrough, so nobody reads the guide to
    find out what follows."""
    console.print(f"\n[cyan]Next[/cyan]  [bold cyan]uv run invoke {command}[/bold cyan]")


def _banner(title: str, body: str = "", style: str = "cyan") -> None:
    text = f"[bold {style}]{title}[/bold {style}]"
    if body:
        text += f"\n{body}"
    console.print()
    console.print(Panel(text, border_style=style, box=box.SIMPLE))


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


TASK_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Get a stack running", ("init", "info", "start", "stop", "restart", "destroy", "build")),
    ("Load the model and the data", ("load", "load-schema", "load-menu", "load-objects", "load-repository")),
    ("The walkthrough", ("demo-setup", "demo", *WALKTHROUGH, "demo-budget", "demo-drift", "demo-raman", "demo-clean")),
    ("Developer tools", ("branch-create", "branch-list", "branch-delete", "check", "docs", "schema-vendor-diff")),
)
"""The order `invoke list` prints, and what belongs beside what.

The branch commands are developer tools and are grouped as such. Printed beside
the scenarios they read as something the walkthrough needs, and it does not:
every task in the walkthrough group defaults its branch.
"""


@task(name="list")
def list_tasks(context: Context) -> None:  # noqa: ARG001
    """Print every task with its description, grouped by what it is for."""
    collection = Collection.from_module(sys.modules[__name__])

    def summary(name: str) -> str:
        return (collection[name].__doc__ or "No description.").strip().splitlines()[0]

    grouped = {name for _, names in TASK_GROUPS for name in names}
    remainder = ("Tests and linters", tuple(sorted(set(collection.task_names) - grouped - {"list"})))

    console.print()
    for title, names in (*TASK_GROUPS, remainder):
        table = Table(title=title, box=box.SIMPLE, header_style="bold cyan", title_justify="left")
        table.add_column("Task", style="green", no_wrap=True)
        table.add_column("What it does", style="white")
        for name in names:
            table.add_row(name, summary(name))
        console.print(table)

    console.print("  Every task takes [cyan]--help[/cyan]. Start with [bold cyan]uv run invoke init[/bold cyan],")
    console.print(
        "  then [bold cyan]uv run invoke demo-setup[/bold cyan] and [bold cyan]uv run invoke demo[/bold cyan].\n"
    )


@task
def info(context: Context, branch: str = "main") -> None:  # noqa: ARG001
    """Show the address, the version, what is running and what is loaded."""
    summary = _get("/api/schema/summary")
    reachable = summary is not None

    lines = [
        f"[cyan]Address[/cyan]        {_address()}",
        f"[cyan]Base version[/cyan]   {BASE_VERSION}",
        f"[cyan]Image[/cyan]          {IMAGE} "
        + ("[green]present[/green]" if _image_exists() else "[yellow]not built[/yellow]"),
        "[cyan]API token[/cyan]      " + ("[green]set[/green]" if _token() else "[red]missing[/red]"),
        "[cyan]Stack[/cyan]          " + ("[green]answering[/green]" if reachable else "[red]not answering[/red]"),
    ]

    if reachable:
        kinds = len(summary.get("nodes", {}))
        sites = _count("OtnSite", branch)
        carriers = _count("OtnOpticalCarrier", branch)
        lines.append(f"[cyan]Branch[/cyan]         {branch}")
        lines.append(f"[cyan]Kinds[/cyan]          {kinds}")
        if sites:
            lines.append(f"[cyan]Dataset[/cyan]        {sites} sites, {carriers} wavelengths")
        else:
            lines.append("[cyan]Dataset[/cyan]        not loaded on this branch (run `invoke load`)")

    console.print()
    console.print(Panel("\n".join(lines), title="[bold]infrahub-demo-otn[/bold]", border_style="blue", box=box.SIMPLE))
    if not reachable:
        console.print("  The stack is down. Run [bold cyan]uv run invoke start[/bold cyan].\n")
    else:
        console.print()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


@task
def build(context: Context, no_cache: bool = False) -> None:
    """Build the Infrahub image with infrahub_demo_otn installed."""
    _banner("Building", f"[dim]{IMAGE}[/dim]", "green")
    command = f"{COMPOSE} build"
    if no_cache:
        command += " --no-cache"
    context.run(command, pty=True)
    console.print(f"[green]ok[/green] {IMAGE}")


@task
def start(context: Context, rebuild: bool = False) -> None:
    """Start the stack. Builds the image first when it is missing."""
    if rebuild or not _image_exists():
        build(context, no_cache=False)
    _banner("Starting Infrahub", f"[dim]{_address()}[/dim]", "green")
    # Before compose, not after. `./.remote` is a bind mount source, and Docker
    # creates a missing one itself, as root when the daemon is rootful. The
    # export then cannot write into it and `invoke init` fails on the one
    # command the docs tell a reader to run.
    REMOTE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    context.run(f"{COMPOSE} up -d", pty=True)
    console.print(f"[green]ok[/green] Infrahub is starting at {_address()}")
    console.print("  It takes about a minute to answer. Check with [cyan]uv run invoke info[/cyan].")


@task
def stop(context: Context) -> None:
    """Stop the stack. Volumes and data survive."""
    _banner("Stopping", style="yellow")
    context.run(f"{COMPOSE} down", pty=True)
    console.print("[green]ok[/green] stopped, data kept")


@task
def restart(context: Context, component: str = "") -> None:
    """Restart the stack, or one service with --component. Never rebuilds."""
    target = f" {component}" if component else ""
    _banner("Restarting", f"[dim]{component or 'every service'}[/dim]", "yellow")
    context.run(f"{COMPOSE} restart{target}", pty=True)
    console.print("[green]ok[/green] restarted")


@task
def destroy(context: Context) -> None:
    """Stop the stack and delete its volumes. Every loaded object goes."""
    _banner("Destroying", "[red]Containers and volumes. Every loaded object goes.[/red]", "red")
    context.run(f"{COMPOSE} down -v", pty=True)
    console.print("[green]ok[/green] destroyed")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


@task
def load_schema(context: Context, branch: str = "main") -> None:
    """Load schemas/ onto a branch."""
    _ctl(context, f"schema load schemas/ --branch {branch}")


@task
def load_menu(context: Context, branch: str = "main") -> None:
    """Replace the sidebar on a branch with menus/otn.yml."""
    removed = _purge_menu(branch)
    if removed:
        console.print(f"[yellow]-[/yellow] removed {removed} existing OTN menu items, so the file is the sidebar")
    _ctl(context, f"menu load menus/ --branch {branch}")


@task
def load_objects(context: Context, branch: str = "main", file: str = "") -> None:
    """Load one object file onto a branch with --file, or all of objects/ without it."""
    _ctl(context, f"object load {file or 'objects/'} --branch {branch}")


def _export_committed_tree() -> str:
    """Write the committed tree into `.remote/<name>` and return its commit hash.

    `git archive HEAD` rather than a copy of the working tree, for two reasons.
    What Infrahub imports should be what is committed, and a copy would carry
    `.venv/`, `docs/node_modules/` and both caches into a directory the
    containers then have to read.

    The export is a git repository because Infrahub clones it. A directory of
    files is not something it can pull a commit out of.

    **The history is kept across runs.** Deleting `.git` and running `git init`
    again would publish an unrelated root commit under a location an already
    registered repository has cloned, which is not a fetch Infrahub can follow.
    So the second run replaces the files and commits on top, and the caller
    waits for that specific hash to arrive rather than for a status that was
    already `in-sync` before it started.
    """
    destination = REMOTE_DIRECTORY / REPOSITORY_NAME
    git = ["git", "-c", "user.name=infrahub", "-c", "user.email=no-reply@opsmill.com"]

    if (destination / ".git").is_dir():
        # Clear the working tree and leave `.git` in place. `git rm` rather than
        # rmtree so the index matches, and a file deleted upstream disappears
        # here too instead of lingering for the workers to import.
        # `check=False`. An interrupted earlier run can leave the index empty,
        # and `git rm --cached .` against an empty index exits 128 on a pathspec
        # that matched nothing. With check=True that state is unrecoverable:
        # every later run aborts here, and the only documented escape is
        # deleting `.remote/`, which this function exists to avoid.
        subprocess.run([*git, "rm", "-r", "-q", "--cached", "."], cwd=destination, check=False)  # noqa: S603
        for entry in destination.iterdir():
            if entry.name == ".git":
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    else:
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        subprocess.run([*git, "init", "-q", "-b", "main"], cwd=destination, check=True)  # noqa: S603

    archive = subprocess.run(  # noqa: S603
        ["git", "archive", "--format=tar", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    subprocess.run(  # noqa: S603
        ["tar", "-x", "-C", str(destination)],  # noqa: S607
        input=archive.stdout,
        check=True,
    )

    # Identity on the command line, not from the developer's global config: the
    # commit is a build product and should not be attributed to whoever ran it.
    subprocess.run([*git, "add", "-A"], cwd=destination, check=True)  # noqa: S603
    head = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # No `--allow-empty`. An unchanged tree has to keep its hash, because the
    # caller compares that hash against what the server holds to decide whether
    # there is anything to publish. A new hash every run would make every run
    # look like a change.
    staged = subprocess.run(  # noqa: S603
        [*git, "diff", "--cached", "--quiet"],
        cwd=destination,
        capture_output=True,
        check=False,
    )
    unborn = subprocess.run(  # noqa: S603
        [*git, "rev-parse", "--verify", "-q", "HEAD"],
        cwd=destination,
        capture_output=True,
        check=False,
    )
    if staged.returncode != 0 or unborn.returncode != 0:
        subprocess.run(  # noqa: S603
            [*git, "commit", "-q", "-m", f"Export of {head}"],
            cwd=destination,
            check=True,
        )

    return subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=destination,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _working_tree_is_dirty() -> bool:
    """Whether anything is uncommitted. The export would not carry it."""
    result = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def _remote_is_mounted() -> bool:
    """Whether the running server can see `/remote`.

    Checked rather than assumed. Adding the mount to the override file does
    nothing for containers that were already running when it was added, and the
    failure that produces is a repository stuck in `error-import`.
    """
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(  # noqa: S603
        ["docker", "exec", f"{PROJECT}-infrahub-server-1", "test", "-d", REMOTE_MOUNT],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _repository_status(branch: str) -> tuple[str, str, str] | None:
    """The id, sync status and imported commit, or `None` when there is none."""
    data = _graphql(
        f'{{ CoreRepository(name__value: "{REPOSITORY_NAME}") '
        "{ count edges { node { id sync_status { value } commit { value } } } } }",
        branch,
    )
    repositories = data["CoreRepository"]
    if not repositories["count"]:
        return None
    node = repositories["edges"][0]["node"]
    return str(node["id"]), str(node["sync_status"]["value"]), str(node["commit"]["value"] or "")


def _commit_is_in_export(commit: str) -> bool:
    """Whether the export's history contains the commit Infrahub imported.

    It will not when `.remote` was rebuilt from scratch, which leaves the server
    holding a commit from a history that no longer exists. Infrahub cannot fetch
    forward from a commit its remote has never heard of, so the import sits at
    the old one and reports `in-sync` about it forever.
    """
    if not commit:
        return False
    result = subprocess.run(  # noqa: S603
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],  # noqa: S607
        cwd=REMOTE_DIRECTORY / REPOSITORY_NAME,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _report_what_the_sync_created(branch: str) -> None:
    """Print the definitions the import produced, and fail when any are missing.

    The point of the whole task. A repository that reaches in-sync and creates
    no definitions is the failure this project has hit before, twice, and it is
    invisible unless something counts what arrived.

    Counted against `.infrahub.yml` rather than against a number written here,
    so adding a tenth transform does not quietly leave this check asserting
    nine. `expected` is what the file registers; anything less means the import
    read the file and dropped part of it.
    """
    from infrahub_sdk.ctl.repository import get_repository_config

    config = get_repository_config(REPO_ROOT / ".infrahub.yml")
    expected = {
        "CoreCheckDefinition": len(config.check_definitions),
        "CoreTransformPython": len(config.python_transforms),
        "CoreArtifactDefinition": len(config.artifact_definitions),
        "CoreGeneratorDefinition": len(config.generator_definitions),
        "CoreGraphQLQuery": len(config.queries),
    }

    data = _graphql(
        """{
          CoreCheckDefinition { count }
          CoreTransformPython { count }
          CoreArtifactDefinition { count edges { node { name { value } } } }
          CoreGeneratorDefinition { count }
          CoreGraphQLQuery { count }
        }""",
        branch,
    )

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("What the sync created", style="green", no_wrap=True)
    table.add_column("Count", style="white")
    table.add_column("Registered", style="white")

    short: list[str] = []
    for label, kind in (
        ("Check definitions", "CoreCheckDefinition"),
        ("Python transforms", "CoreTransformPython"),
        ("Artifact definitions", "CoreArtifactDefinition"),
        ("Generator definitions", "CoreGeneratorDefinition"),
        ("GraphQL queries", "CoreGraphQLQuery"),
    ):
        found = int(data[kind]["count"])
        want = expected[kind]
        table.add_row(label, str(found), str(want))
        if found < want:
            short.append(f"{label.lower()}: {found} of {want}")
    console.print(table)

    artifacts = [edge["node"]["name"]["value"] for edge in data["CoreArtifactDefinition"]["edges"]]
    if artifacts:
        console.print(f"  Artifact definitions: [cyan]{', '.join(sorted(artifacts))}[/cyan]")

    if short:
        _fail(
            "The import reached in-sync and did not create everything .infrahub.yml registers: "
            + "; ".join(short)
            + ". `docker logs infrahub-demo-otn-task-worker-1` says which entry it rejected."
        )


@task(name="load-repository")
def load_repository(context: Context, branch: str = "main", timeout: int = 300) -> None:  # noqa: ARG001
    """Register this repository with the stack and wait for the import.

    This is what turns the proposed-change pipeline on. Until it has run, the
    checks are only ever terminal output and no artifact definition exists, so
    nothing renders a file onto an object.

    Idempotent. A second run commits the current tree on top of the existing
    export and waits for that commit to arrive, rather than for a status that
    was already `in-sync` when it started.
    """
    _require_stack()

    if shutil.which("docker") is None:
        _fail("No `docker` on PATH, so the mount the containers read cannot be checked.")

    if not _remote_is_mounted():
        _fail(
            f"The server cannot see {REMOTE_MOUNT}. The containers were started before the mount was added.",
            "start --rebuild",
        )

    _banner("Registering the repository", f"[dim]branch {branch}[/dim]")

    if _working_tree_is_dirty():
        # `git archive HEAD` carries what is committed and nothing else, so an
        # uncommitted edit would import as its old version and the run would
        # still say `ok`. Worth a line rather than a silent surprise.
        console.print("[yellow]![/yellow] the working tree is dirty; only committed files are exported")

    console.print("[cyan]1/3[/cyan] exporting the committed tree")
    exported = _export_committed_tree()
    console.print(f"  [dim]{REMOTE_DIRECTORY / REPOSITORY_NAME} -> {REMOTE_MOUNT}/{REPOSITORY_NAME}[/dim]")
    console.print(f"  [dim]export commit {exported[:8]}[/dim]")

    existing = _repository_status(branch)

    if existing is not None and existing[1] == "in-sync" and existing[2] == exported:
        console.print(f"[cyan]2/3[/cyan] already in-sync at {exported[:8]}, nothing to publish")
        console.print("[cyan]3/3[/cyan] no import to wait for\n")
        _report_what_the_sync_created(branch)
        _next_step("demo-setup")
        return

    # There is something to publish and the repository already exists. Infrahub
    # fetches a registered repository on its own schedule and
    # `InfrahubRepositoryProcess` re-reads what it already has rather than going
    # back to the remote, so asking politely does not move it. Recreating the
    # object does, immediately, and it costs nothing that the import does not
    # put straight back. It also covers the case where `.remote` was rebuilt and
    # the server is holding a commit from a history that no longer exists.
    if existing is not None:
        held = existing[2][:8] or "nothing"
        reason = "from a history this export no longer has" if not _commit_is_in_export(existing[2]) else "out of date"
        console.print(f"  [yellow]![/yellow] the server holds {held}, {reason}, so recreating the repository object")
        _graphql(f'mutation {{ CoreRepositoryDelete(data: {{ id: "{existing[0]}" }}) {{ ok }} }}', branch)
        existing = None

    console.print("[cyan]2/3[/cyan] creating the repository object")
    _graphql(
        "mutation { CoreRepositoryCreate(data: {"
        f'name: {{ value: "{REPOSITORY_NAME}" }}, '
        f'location: {{ value: "{REMOTE_MOUNT}/{REPOSITORY_NAME}" }}'
        "}) { ok } }",
        branch,
    )

    console.print(f"[cyan]3/3[/cyan] waiting for the import to reach {exported[:8]}, up to {timeout}s")
    deadline = time.monotonic() + timeout
    state: tuple[str, str, str] | None = None
    while time.monotonic() < deadline:
        state = _repository_status(branch)
        # Both conditions. `in-sync` alone is what the previous run left behind,
        # so a task that stops there reports success without having published
        # anything, which is the failure this whole thing is built to prevent.
        if state is not None and state[1] == "in-sync" and state[2] == exported:
            break
        if state is not None and state[1] == "error-import":
            _fail(
                "The repository import failed. `docker logs infrahub-demo-otn-task-worker-1` says why. "
                "A syntax error in .infrahub.yml fails the whole file."
            )
        time.sleep(5)

    if state is None or state[1] != "in-sync" or state[2] != exported:
        seen = "no repository object" if state is None else f"status {state[1]!r} at commit {state[2][:8] or 'none'}"
        _fail(f"The import did not reach {exported[:8]} in {timeout}s ({seen}).")

    console.print(f"[green]ok[/green] in-sync at {exported[:8]}\n")
    _report_what_the_sync_created(branch)
    _next_step("demo-setup")


@task
def load(context: Context, branch: str = "main") -> None:
    """Load the schema, the menu and the dataset, in that order."""
    _banner("Loading", f"[dim]branch {branch}[/dim]")
    console.print("[cyan]1/3[/cyan] schema")
    load_schema(context, branch)
    console.print("[cyan]2/3[/cyan] menu")
    load_menu(context, branch)
    console.print("[cyan]3/3[/cyan] objects, about a minute for 2344 of them")
    load_objects(context, branch)
    console.print(f"[green]ok[/green] loaded onto {branch}")


@task
def init(context: Context) -> None:
    """From nothing to a working demo: destroy, start, load."""
    _banner(
        "Initialize",
        "[yellow]This deletes every object in the current stack.[/yellow]\n\n"
        "  1. destroy the containers and their volumes\n"
        "  2. start a new stack\n"
        "  3. load the schema, the menu and the dataset onto main\n"
        "  4. register the repository, which turns the pipeline on",
        "magenta",
    )
    destroy(context)
    start(context)
    console.print("\n[yellow]Waiting for the server to answer[/yellow]")
    if not _wait_for_stack():
        _fail("The stack did not answer in five minutes.", "info")
    load(context)
    # Registration is part of getting to a working demo, not an extra step. The
    # checks, the transforms and the artifact definitions only exist once the
    # repository has been imported, and a demo where the pipeline does nothing
    # is a demo missing the half Infrahub is for.
    load_repository(context)
    _banner(
        "Ready",
        f"[cyan]Infrahub[/cyan]  {_address()}\n[cyan]Next[/cyan]      uv run invoke demo-setup",
        "green",
    )


def _wait_for_stack(attempts: int = 60, interval: float = 5.0) -> bool:
    for _ in range(attempts):
        if _get("/api/schema/summary", timeout=3.0) is not None:
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# Branches
# --------------------------------------------------------------------------- #


@task
def branch_create(context: Context, name: str, sync_with_git: bool = True) -> None:
    """Create a branch, in Git as well as in the graph.

    `infrahubctl` defaults the other way, and a branch that does not exist in
    Git runs the two built-in validators instead of this repository's checks.
    """
    flag = "--sync-with-git" if sync_with_git else "--no-sync-with-git"
    _ctl(context, f"branch create {name} {flag}")


@task
def branch_list(context: Context) -> None:
    """List the branches."""
    _ctl(context, "branch list")


@task
def branch_delete(context: Context, name: str) -> None:
    """Delete a branch."""
    _ctl(context, f"branch delete {name}")


# --------------------------------------------------------------------------- #
# Checks, tests, linters
# --------------------------------------------------------------------------- #


@task
def check(context: Context, branch: str = "main", name: str = "") -> None:
    """Run the repository checks, or one of them with --name."""
    targets = (name,) if name else CHECKS
    if name and name not in CHECKS:
        _fail(f"No check called {name!r}. There are {len(CHECKS)}: {', '.join(CHECKS)}.")
    for target in targets:
        console.print(f"\n[cyan]->[/cyan] {target}")
        _ctl(context, f"check {target} --branch {branch}", warn=True)


@task
def test_unit(context: Context) -> None:
    """Run the unit tests. No Infrahub, about two seconds."""
    context.run("uv run pytest tests/unit -v", pty=True)


@task
def test_integration(context: Context) -> None:
    """Run the integration tests.

    This starts its own throwaway Infrahub through testcontainers, on its own
    ports and its own database.

    **Stop the demo stack first, with `invoke stop`.** The two do not collide on
    a port, but they do collide on memory: during feature 016 the pair exhausted
    the container runtime and the test database was killed, failing twelve of
    thirteen tests for a reason that had nothing to do with the code. `invoke
    stop` keeps the volumes, so `invoke start` brings the demo back as it was.
    """
    if not _image_exists():
        _fail(f"The image {IMAGE} is missing.", "build")
    context.run("uv run pytest tests/integration -v", pty=True)


@task
def test(context: Context) -> None:
    """Run the unit tests, then the integration tests."""
    test_unit(context)
    test_integration(context)


@task(name="format")
def format_code(context: Context) -> None:
    """Format every Python file with ruff."""
    with context.cd(REPO_ROOT):
        context.run("uv run ruff format .", pty=True)
        context.run("uv run ruff check . --fix", pty=True)


@task
def lint_format(context: Context) -> None:
    """Check Python formatting with ruff."""
    context.run("uv run ruff format --check --diff", pty=True)


@task
def lint_ruff(context: Context) -> None:
    """Lint Python with ruff."""
    context.run("uv run ruff check", pty=True)


@task
def lint_mypy(context: Context) -> None:
    """Type-check src, tests and scripts with mypy."""
    context.run("uv run mypy src tests scripts", pty=True)


@task
def lint_yaml(context: Context) -> None:
    """Lint YAML with yamllint."""
    context.run("uv run yamllint -s .", pty=True)


@task
def lint_markdown(context: Context) -> None:
    """Lint Markdown with rumdl."""
    context.run("uv run rumdl check .", pty=True)


VALE_PATHS = (
    "README.md",
    "docs/docs",
    "src",
    "tests",
    "scripts",
    "tasks.py",
    "checks",
    "generators",
    "transforms",
    "queries",
    "schemas",
    "objects",
    "menus",
    "demo",
)
"""What `lint-prose` reads.

Every shipped path, not only the documentation. The em dash rule and the
excluded-vocabulary rule apply to code comments and docstrings too, and
`.vale.ini` switches the rest of the Infrahub style off outside `docs/` and
`README.md` so a Python identifier is not reported as a spelling error.
"""


@task
def lint_prose(context: Context) -> None:
    """Lint prose across the tree with vale. Needs vale on your PATH."""
    if shutil.which("vale") is None:
        _fail("vale is not on your PATH. See https://vale.sh for how to install it.")
    context.run(f"vale --glob='!*.pyc' {' '.join(VALE_PATHS)}", pty=True)


@task
def lint(context: Context) -> None:
    """Run every linter CI runs."""
    _banner("Linting", "[dim]ruff format, ruff, mypy, yamllint, rumdl[/dim]", "yellow")
    lint_format(context)
    lint_ruff(context)
    lint_mypy(context)
    lint_yaml(context)
    lint_markdown(context)
    console.print("[green]ok[/green] every linter passed")


@task
def schema_check(context: Context) -> None:
    """Check schema formatting. Offline, no Infrahub needed."""
    context.run("uv run infrahubctl schema format --check schemas/", pty=True)


@task
def schema_vendor_diff(context: Context) -> None:  # noqa: ARG001
    """Diff the vendored location schema against the upstream ref it names.

    `schemas/location.yml` holds LocationGeneric copied from
    opsmill/schema-library. A hand-copied schema cannot be diffed against
    upstream, so it diverges silently, and this is what makes the divergence
    visible. Three differences are deliberate and listed in the file header.

    Not part of `lint`: it needs the network, and a gate that needs the network
    is a gate that fails on a plane.
    """
    _banner("Vendor diff", f"[dim]{VENDOR_SOURCE} at {VENDOR_REF}[/dim]")
    try:
        response = httpx.get(VENDOR_URL, timeout=20.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as error:
        _fail(f"Could not reach {VENDOR_URL}: {error}. This task needs the network.")
        return

    upstream = (REPO_ROOT / ".vendor-location-upstream.yml").with_suffix(".yml")
    upstream.write_text(response.text)
    try:
        result = context.run(f"diff -u {upstream} schemas/location.yml", pty=True, warn=True)
    finally:
        upstream.unlink(missing_ok=True)

    if result.exited == 0:
        console.print("[green]ok[/green] the vendored file is identical to upstream")
    else:
        console.print(
            "\n[yellow]-[/yellow] differences above. Three are deliberate and named in the header of "
            "schemas/location.yml: LocationHosting is not taken, include_in_menu is false, and "
            "display_labels is replaced by display_label. Anything else is drift."
        )


@task
def docs(context: Context) -> None:
    """Build the documentation site."""
    with context.cd(DOCS_DIRECTORY):
        context.run("pnpm build", pty=True)


# --------------------------------------------------------------------------- #
# Demo scenarios
#
# One task per step of the three walkthrough pages under docs/docs/:
# provisioning-scenarios.mdx for the two that write, reporting-scenarios.mdx for
# the six that read, and loadable-scenarios.mdx for the loadable pairs and the
# runbook. WALKTHROUGH below is the runbook order, and `demo` runs it.
#
# Every task here defaults its branch, opens with a precondition that names the
# task to run first, and closes by naming the task that comes next. Nobody
# should have to read the guide to find out what to type.
# --------------------------------------------------------------------------- #


@task
def demo_setup(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Set the demo up on a branch: dataset and the five services."""
    _banner("Demo setup", f"[dim]branch {branch}[/dim]")
    _ensure_dataset(context, branch)

    # A branch can hold the dataset and not the generator target group, so this
    # one file is loaded on both paths. It is idempotent.
    _ctl(context, f"object load objects/00_groups.yml --branch {branch}")

    # Only the memberships are scenario data; the group itself came from the
    # dataset load above.
    console.print("[cyan]->[/cyan] the services that join the generator target group")
    for path in _scenario("demo-setup").files:
        _ctl(context, f"object load {path} --branch {branch}")

    _banner("Ready", "[cyan]Next[/cyan]  uv run invoke demo-capacity", "green")


@task
def demo_capacity(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Scenario 3: what spectrum is left, and what can use it."""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform capacity_view --branch {branch}")
    _next_step("demo-reach")


@task
def demo_reach(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Scenario 6: where the cheap pluggables reach. Nowhere, and why."""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform reach_report --branch {branch}")
    _next_step("demo-provision")


@task
def demo_provision(context: Context, branch: str = DEMO_BRANCH, service: str = "svc-ber-ams-400g") -> None:
    """Scenario 1: provision Berlin to Amsterdam at 400G."""
    _ensure_dataset(context, branch)
    _ctl(context, f"generator optical_service --branch {branch} service={service}")
    _next_step("demo-provision-all")


@task
def demo_provision_all(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Provision the four remaining demo services."""
    _ensure_dataset(context, branch)
    for service in DEMO_SERVICES[1:]:
        console.print(f"\n[cyan]->[/cyan] {service}")
        _ctl(context, f"generator optical_service --branch {branch} service={service}", warn=True)
    _next_step("demo-trace")


@task
def demo_trace(context: Context, branch: str = DEMO_BRANCH, service: str = "svc-ams-mil-ai-400g") -> None:
    """Scenario 5: trace a service end to end, router to glass to router."""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform service_trace --branch {branch} service={service}")
    _next_step("demo-impact")


@task
def demo_impact(context: Context, branch: str = DEMO_BRANCH, section: str = "oms-ams-fra") -> None:
    """Scenario 4: cut the Frankfurt to Amsterdam fiber. What goes down?"""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform impact_report --branch {branch} section={section}")
    _next_step("demo-srlg")


@task
def demo_budget(context: Context, branch: str = DEMO_BRANCH) -> None:
    """The link budget for every wavelength on a branch, worst margin first."""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform budget_report --branch {branch}")
    _next_step("demo-latency")


@task
def demo_srlg(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Scenario 7: which services share a duct and are not diverse."""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform srlg_exposure --branch {branch}")
    _next_step("demo-latency")


@task
def demo_latency(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Scenario 8: the AI and HPC services against their latency budgets."""
    _ensure_dataset(context, branch)
    _ctl(context, f"transform ai_latency --branch {branch}")
    _next_step("demo-infiniband")


@task
def demo_refusal(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Scenario 2: fill both layers of one corridor, then ask twice.

    Two services on the same route with opposite answers, which is the pair the
    scenario exists for. The 400G is refused because the roomiest wavelength on
    the corridor offers 240 free slots and its client needs 320, and no
    wavelength can be lit because the spectrum is gone. The 100G then provisions
    on that same corridor, grooming into one of those wavelengths and consuming
    no channel at all.

    The 400G runs first on purpose. It reports the tightest container it did not
    fit, and running the 100G first would groom eighty slots into that container
    and move the figure the refusal quotes.

    **The branch this leaves behind merges, on an accepted refusal.**
    `demo/90_fra_mil_saturated.yml` sets `refusal_accepted` on the 400G, because
    that file is what takes the corridor away from a service defined in
    `demo/00_services.yml`. `checks/provisionable.py` reads the refusal, sees it
    signed for and says nothing, so the walkthrough ends green. The scenario is
    about two answers on one corridor, and a red pipeline would say the second
    answer is a fault.

    The scenario that ends in a **blocked** merge is Madrid to Warsaw,
    `demo/06_mad_waw_16qam.yml`, which is hand-loaded and not part of this
    walkthrough. It signs for nothing, so the gate is seen firing there.
    """
    _banner("Congestion in two layers", "[dim]Fill Frankfurt to Milan, then ask twice[/dim]", "magenta")
    _ensure_dataset(context, branch)
    _ctl(context, f"object load demo/90_fra_mil_saturated.yml --branch {branch}")
    _ctl(context, f"generator optical_service --branch {branch} service=svc-fra-mil-ai-400g", warn=True)
    _ctl(context, f"generator optical_service --branch {branch} service=svc-fra-mil-transit-100g")
    console.print(
        "\n  Refused on slots, not on spectrum and not on latency. The 400G needs a\n"
        "  whole ODUC4 and the roomiest wavelength there has 240 of 320 free. The\n"
        "  100G needs 80 and provisions on the same corridor without lighting\n"
        "  anything, because a corridor with no room for a carrier still has room\n"
        "  for a tenant. 53 of the 96 anchors carry nothing there and not one of\n"
        "  them is usable: the widest free block is 38,000 MHz and the narrowest\n"
        "  mode in the catalog occupies 44,400.\n"
        "\n"
        "  This branch merges. The refusal is accepted: demo/90_fra_mil_saturated.yml\n"
        "  sets refusal_accepted on the 400G, so checks/provisionable.py reads the\n"
        "  verdict, finds a person signed for it and stays quiet. Nine of the ten\n"
        "  scenarios refuse nothing at all; this one refuses and keeps the record.\n"
        "  The scenario that blocks a merge is Madrid to Warsaw, loaded by hand from\n"
        "  demo/06_mad_waw_16qam.yml, which signs for nothing on purpose."
    )
    _next_step("demo-clean")


@task
def demo_infiniband(context: Context, branch: str = DEMO_BRANCH) -> None:
    """Scenario 9: the one service that states its own client signal.

    Every other scenario service leaves the handover to the rate rule, which
    walks an allow-list of Ethernet, SDH, PDH and Fibre Channel and never
    reaches an InfiniBand row. Frankfurt to Prague states `IB-HDR-4X` and the
    generator writes an `ODUflex` for it. Drop that one line from the file and
    the same request provisions `400GBASE-FR4` instead, with no error.

    The request is 212 Gbps because HDR signals at 212.5, which is 170 tributary
    slots, and a 200G wavelength offers 160. The service therefore lands on a
    400G mode, whose `ODUC4` offers 320.
    """
    _banner("InfiniBand handover", f"[dim]branch {branch}[/dim]", "magenta")
    _ensure_dataset(context, branch)
    _ctl(context, f"object load demo/03_infiniband_service.yml --branch {branch}")
    _ctl(context, f"generator optical_service --branch {branch} service=svc-fra-prg-ib-212g")
    console.print(
        "\n  212.5 Gbps of signalling, so the container is flexible: an ODUflex of\n"
        "  170 slots in the 320 an ODUC4 offers, leaving 150 free. 170 would not\n"
        "  fit the 160 a 200G wavelength offers, which is why this one is 400G.\n"
        f"  Read it back at {_address()}/graphql/{branch}."
    )
    _next_step("demo-refusal")


@task
def demo_drift(context: Context, branch: str = DEMO_BRANCH) -> None:
    """What the equipment reports against what the model says it should.

    Every amplifier and pump carries a monitor holding its measured gain. This
    compares each one against the gain the device is configured for and lists
    the ones outside tolerance. The dataset seeds a droop, so the report has
    something to find.
    """
    _ensure_dataset(context, branch)
    _ctl(context, f"transform monitor_drift --branch {branch}")
    _next_step("demo-clean")


@task
def demo_raman(context: Context, branch: str = RAMAN_BRANCH) -> None:
    """The Paris to Madrid answer: pumps on a branch, and the check turns green.

    The default branch keeps the red check, so this puts the fix on a branch of
    its own and the two runs can be compared.
    """
    _banner("Raman on Paris to Madrid", f"[dim]branch {branch}[/dim]", "magenta")

    # The dataset has to be on the branch this forks from, not on the branch it
    # is about to create.
    _ensure_dataset(context, branch if _branch_exists(branch) else "main")
    _ensure_branch(context, branch)

    scenario = _scenario("demo-raman")
    for path in scenario.files:
        _ctl(context, f"object load {path} --branch {branch}")

    console.print("\n[cyan]->[/cyan] the same check that fails on the default branch")
    _ctl(context, f"check {scenario.check} --branch {branch}", warn=True)
    console.print(
        "\n  Six pump objects and no edited margin. Open a proposed change from\n"
        f"  {branch} and the diff says the same thing."
    )
    # The one place the walkthrough prints a branch name, because this scenario
    # is the comparison between two branches and hiding the second hides it.
    _next_step(f"demo-clean --branch {branch}")


@task(name="demo")
def demo_all(context: Context, branch: str = DEMO_BRANCH) -> None:
    """The whole walkthrough, in the order the guide runs it.

    Nine numbered scenarios in ten steps, back to back, about twenty minutes of
    output. The tenth step is `demo-provision-all`, which carries no scenario
    number: it provisions the four services the later scenarios read, so it is
    setup inside the walkthrough rather than something a presenter narrates.
    `demo-guide.mdx` counts the nine and this runs the ten.

    Run `demo-setup` first; each step is also runnable on its own and names the
    one that follows it.

    **How this walkthrough ends, since `checks/provisionable.py` now gates on
    it.** Nine of the ten scenarios refuse nothing, so there is nothing on them
    for the check to read. The tenth, `demo-refusal`, refuses
    `svc-fra-mil-ai-400g` on slots and accepts that refusal in
    `demo/90_fra_mil_saturated.yml`, so the branch merges with the refusal on the
    record. No scenario in this walkthrough blocks a merge; the one that does is
    `demo/06_mad_waw_16qam.yml`, loaded by hand onto its own branch.

    The walkthrough itself still exits 0 either way. It runs generators and
    transforms, not checks, and a blocked merge is a proposed change going red
    rather than a task failing.
    """
    _banner("The walkthrough", f"[dim]{len(WALKTHROUGH)} steps on branch {branch}[/dim]", "magenta")
    _ensure_dataset(context, branch)

    collection = Collection.from_module(sys.modules[__name__])
    for position, name in enumerate(WALKTHROUGH, start=1):
        _banner(f"{position}/{len(WALKTHROUGH)}  {name}", collection[name].__doc__.strip().splitlines()[0])
        collection[name](context, branch=branch)

    _banner("The walkthrough is done", "[cyan]Next[/cyan]  uv run invoke demo-clean", "green")


@task
def demo_clean(context: Context, branch: str = "") -> None:
    """Delete every branch a scenario creates, or one of them with --branch.

    The default branch was never touched. Each branch is named before it goes,
    because a task that deletes several without saying which is one nobody can
    check afterwards.
    """
    _require_stack()
    wanted = (branch,) if branch else tuple(dict.fromkeys(row.branch for row in SCENARIO_BRANCHES))
    present = [name for name in wanted if _branch_exists(name)]
    for name in present:
        console.print(f"[yellow]-[/yellow] deleting branch {name}")
        branch_delete(context, name)
    console.print(f"[green]-[/green] removed {len(present)} of the {len(wanted)} scenario branches")
