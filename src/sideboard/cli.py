"""Sideboard command line.

Every command documents what it touches. ``doctor`` performs only local/config checks
unless an explicit probe flag is given. Nothing here installs, starts or mutates an
external service.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core.config import (
    ConfigError,
    config_from_document,
    load_toml,
    merge_documents,
    render_default_config,
    resolve_config,
    shipped_defaults,
)
from .core.logs import configure_logging, redact
from .core.paths import Paths, default_paths
from .core.registry import RegistryError
from .core.storage import CORE_MIGRATIONS, Database, ensure_private_dir

log = logging.getLogger("sideboard.cli")

__all__ = ["main"]

# Sub-parser handles, so `--help` for a nested command can be printed without exiting.
_SUB_PARSERS: dict[str, Any] = {}


def _print_sub_help(name: str) -> None:
    """Print a nested command's help without argparse's SystemExit."""
    sub = _SUB_PARSERS.get(name)
    if sub is not None:
        sub.print_help()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sideboard",
        description="Local Mac TUI dashboard. No account, key or cloud service required.",
    )
    parser.add_argument("--config", help="path to config.toml (default ~/.config/sideboard)")
    parser.add_argument("--data-dir", help="override the private data directory")
    parser.add_argument("--log-dir", help="override the private log directory")
    parser.add_argument("--demo", action="store_true", help="synthetic data, separate database")
    parser.add_argument(
        "--offline", action="store_true", help="suppress public network access entirely"
    )
    parser.add_argument("--no-mouse", action="store_true", help="disable mouse reporting")
    parser.add_argument("--verbose", action="store_true", help="log to the console as well")
    parser.add_argument("--version", action="store_true", help="print the version and exit")

    sub = parser.add_subparsers(dest="command")
    _SUB_PARSERS["command"] = sub

    init = sub.add_parser("init", help="write a complete editable configuration")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.add_argument("--print", dest="print_only", action="store_true", help="print, do not write")

    doctor = sub.add_parser("doctor", help="local checks; probes only with an explicit flag")
    doctor.add_argument("--mouse", action="store_true", help="interactive mouse/resize diagnostic")
    doctor.add_argument("--network", action="store_true", help="bounded public probes")
    doctor.add_argument("--mac", action="store_true", help="exercise local counters")
    doctor.add_argument("--docker", action="store_true", help="list local Docker contexts only")

    config = sub.add_parser("config", help="configuration utilities")
    config_sub = config.add_subparsers(dest="config_command")
    _SUB_PARSERS["config"] = config_sub
    validate = config_sub.add_parser("validate", help="validate without starting anything")
    validate.add_argument("--show", action="store_true", help="print the resolved summary")

    features = sub.add_parser("features", help="registered types and availability")
    features_sub = features.add_subparsers(dest="features_command")
    _SUB_PARSERS["features"] = features_sub
    listing = features_sub.add_parser("list", help="list source and widget types")
    listing.add_argument("--json", action="store_true")

    themes = sub.add_parser("themes", help="colour palettes and their override tokens")
    themes_sub = themes.add_subparsers(dest="themes_command")
    _SUB_PARSERS["themes"] = themes_sub
    themes_sub.add_parser("list", help="list palettes, or show one palette's tokens")
    themes_sub.add_parser("show", help="every token of one palette").add_argument("name")

    import_cmd = sub.add_parser("import", help="preview, then commit a local file import")
    import_sub = import_cmd.add_subparsers(dest="import_kind")
    _SUB_PARSERS["import"] = import_sub
    for kind in ("holdings", "prices", "fx", "youtube"):
        child = import_sub.add_parser(kind, help=f"import {kind}")
        child.add_argument("path")
        child.add_argument("--preview-only", action="store_true", help="show the preview and stop")
        child.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
        child.add_argument("--account", help="limit a holdings import to one account")
        child.add_argument(
            "--allow-older",
            action="store_true",
            help="accept a statement older than the stored one (historical replacement)",
        )
        child.add_argument(
            "--allow-empty",
            action="store_true",
            help="accept a snapshot that replaces an account with no positions",
        )
        child.add_argument("--decimal", choices=("point", "comma"), default="point")
        child.add_argument("--date-order", choices=("iso", "dmy", "mdy"), default="iso")
        child.add_argument("--delimiter", choices=(",", ";", "tab", "auto"), default="auto")

    return parser


def _paths(args: argparse.Namespace) -> Paths:
    return default_paths(config_file=args.config, data_dir=args.data_dir, log_dir=args.log_dir)


def _registry():
    from .features import build_feature_registry

    return build_feature_registry()


def _load_document(paths: Paths) -> dict:
    if paths.config_file.exists():
        return merge_documents(shipped_defaults(), load_toml(paths.config_file))
    return shipped_defaults()


# --------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace, paths: Paths) -> int:
    registry = _registry()
    text = render_default_config(registry)
    if args.print_only:
        sys.stdout.write(text)
        return 0
    if paths.config_file.exists() and not args.force:
        print(f"configuration already exists: {paths.config_file}")
        print("use --force to overwrite, or edit the file directly")
        return 1
    ensure_private_dir(paths.config_file.parent)
    paths.config_file.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        paths.config_file.chmod(0o600)
    print(f"wrote {paths.config_file}")
    print("edit it, then run `uv run sideboard` (or `uv run sideboard config validate`)")
    print(
        "the dashboard starts with zero credentials; optional sources stay disabled until "
        "you configure them"
    )
    return 0


def cmd_config_validate(args: argparse.Namespace, paths: Paths) -> int:
    registry = _registry()
    try:
        document = _load_document(paths)
        parsed = config_from_document(document)
        resolved = resolve_config(parsed, registry, paths=paths)
    except ConfigError as error:
        print(f"invalid configuration: {error.message}", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"configuration is valid: {paths.config_file}")
    print(
        f"  tabs: {len(resolved.tabs)} · sources: {len(resolved.sources)} · "
        f"widgets: {len(resolved.widgets)} · categories: {len(resolved.categories)}"
    )
    if args.show:
        for tab in resolved.tabs:
            print(f"  tab {tab.id:<10} {tab.layout:<14} {', '.join(tab.widgets)}")
        for source_id, instance in resolved.sources.items():
            state = "enabled" if instance.enabled else "disabled"
            print(f"  source {source_id:<16} {instance.type_id:<24} {state}")
    return 0


def cmd_themes_list(_args: argparse.Namespace) -> int:
    """Palette names, and the tokens a configuration may override."""
    from .ui.palettes import DEFAULT_PALETTE, OVERRIDABLE, PALETTES

    print('Colour palettes (app.theme = "<name>")\n')
    for key in sorted(PALETTES):
        marker = " (default)" if key == DEFAULT_PALETTE else ""
        print(f"  {key:<18}{PALETTES[key].label}{marker}")
    print("\nAny colour can be overridden without leaving the palette:")
    print("  [app.theme_colors]")
    print('  dim = "#8a95a1"')
    print(f"\nOverridable tokens: {', '.join(OVERRIDABLE)}")
    print("`sideboard themes show <name>` prints every token of one palette.")
    return 0


def cmd_themes_show(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .ui.palettes import OVERRIDABLE, PALETTES, resolve_palette

    try:
        palette = resolve_palette(args.name)
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    values = asdict(palette)
    print(f"{args.name}: {palette.label}\n")
    for token in OVERRIDABLE:
        print(f"  {token:<16}{values[token]}")
    _ = PALETTES
    return 0


def cmd_features_list(args: argparse.Namespace, _paths: Paths) -> int:
    registry = _registry()
    payload: list[dict[str, object]] = []
    for feature in registry.features:
        entry: dict[str, object] = {
            "feature": feature.id,
            "description": feature.description,
            "sources": [
                {
                    "type": spec.type_id,
                    "payload_schema": spec.payload_schema,
                    "schema_version": spec.payload_schema_version,
                    "capabilities": sorted(str(cap) for cap in spec.capabilities),
                    "requires_setup": spec.requires_setup,
                    "import_driven": spec.import_driven,
                    "demo": spec.demo_builder is not None,
                    "setup_hint": spec.setup_hint,
                }
                for spec in feature.sources
            ],
            "widgets": [
                {
                    "type": spec.type_id,
                    "inputs": [
                        {
                            "name": binding.name,
                            "accepts": sorted(binding.accepts),
                            "required": binding.required,
                        }
                        for binding in spec.inputs
                    ],
                    "description": spec.description,
                }
                for spec in feature.widgets
            ],
        }
        payload.append(entry)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print("Registered feature types (listed without fetching any data)\n")
    for entry in payload:
        print(f"{entry['feature']}: {entry['description']}")
        for source in entry["sources"]:  # type: ignore[union-attr]
            flags = []
            if source["requires_setup"]:
                flags.append("needs-setup")
            if source["import_driven"]:
                flags.append("import-driven")
            flags.append("demo" if source["demo"] else "no-demo-data")
            print(
                f"  source  {source['type']:<24} {source['payload_schema']} "
                f"v{source['schema_version']} [{', '.join(sorted(source['capabilities']))}] "
                f"({', '.join(flags)})"
            )
            if source["setup_hint"]:
                print(f"          setup: {source['setup_hint']}")
        for widget in entry["widgets"]:  # type: ignore[union-attr]
            inputs = ", ".join(
                f"{binding['name']}:{'+'.join(binding['accepts']) or 'local-state'}"
                for binding in widget["inputs"]
            )
            print(f"  widget  {widget['type']:<24} inputs({inputs or 'none'})")
        print()
    return 0


def cmd_doctor(args: argparse.Namespace, paths: Paths) -> int:
    registry = _registry()
    print("Sideboard doctor — local and configuration checks only by default\n")
    print(
        f"  config       {paths.config_file} ({'present' if paths.config_file.exists() else 'missing'})"
    )
    print(f"  data         {paths.data_dir}")
    print(f"  database     {paths.database_file}")
    print(f"  logs         {paths.log_dir}")
    print(f"  python       {sys.version.split()[0]}")

    try:
        document = _load_document(paths)
        parsed = config_from_document(document)
        resolved = resolve_config(parsed, registry, paths=paths)
        print(
            f"  config       valid · {len(resolved.tabs)} tabs · "
            f"{len(resolved.sources)} sources · {len(resolved.widgets)} widgets"
        )
    except ConfigError as error:
        print(f"  config       INVALID: {error.message}")
        for problem in error.problems[:10]:
            print(f"      - {problem}")
        return 1

    enabled = [sid for sid, instance in resolved.sources.items() if instance.enabled]
    print(f"  enabled      {', '.join(enabled) or 'none'}")
    from .core.contracts import setup_required

    unconfigured = [
        sid
        for sid, instance in resolved.sources.items()
        if instance.enabled and setup_required(instance.spec, instance.settings)
    ]
    if unconfigured:
        print(f"  needs setup  {', '.join(unconfigured)}")

    if args.docker:
        return _doctor_docker(resolved, paths)
    if args.mac:
        return _doctor_mac()
    if args.network:
        return asyncio.run(_doctor_network(resolved))
    if args.mouse:
        print(
            "\nMouse diagnostic: run `uv run sideboard` and press Ctrl+D. Hover support\n"
            "cannot be verified from a headless check, and the in-app screen reports how many\n"
            "MouseMove/Enter/Leave/blur events the terminal actually delivers."
        )
        return 0
    print("\nNo probe requested. Add --network, --mac, --docker or --mouse for a bounded check.")
    return 0


def _doctor_mac() -> int:
    print("\nMac counters (read-only, no root):")
    try:
        import psutil

        from .features.mac_system.config import MacSettings
        from .features.mac_system.sampler import MacSampler
    except ImportError as error:
        print(f"  unavailable: {error}")
        return 1
    # The sampler adapter is deliberately duck-typed: psutil ships no type information.
    sampler = MacSampler(psutil=psutil, settings=MacSettings())  # type: ignore[arg-type]
    first = sampler.sample()
    # Counter deltas need two samples separated by real time, so wait briefly.
    time.sleep(0.5)
    print(
        f"  cpu            {first.cpu.utilisation_percent if first.cpu else None} ({first.cpu.note if first.cpu else ''})"
    )
    second = sampler.sample()
    cpu = second.cpu
    print(f"  cpu (2nd)      {cpu.utilisation_percent if cpu else None}% on a 0–100 % host scale")
    memory = second.memory
    print(
        f"  ram            {memory.utilisation_percent:.1f}% "
        f"({memory.available_bytes} bytes available)"
        if memory
        else "  ram            unavailable"
    )
    print(f"  swap           {second.swap.used_bytes if second.swap else None} bytes in use")
    for disk in second.disks:
        print(f"  disk {disk.mount:<8} {disk.free_bytes} bytes free")
    network = second.network
    if network:
        print(
            f"  net            {network.interfaces} rx={network.receive_bps} tx={network.send_bps} "
            f"({network.note or 'delta over ' + format(network.interval_seconds or 0, '.1f') + 's'})"
        )
    battery = second.battery
    if battery:
        print(
            f"  battery        present={battery.present} percent={battery.percent} "
            f"plugged={battery.plugged_in} ({battery.note or 'ok'})"
        )
    print(
        "  not available  temperature, fans, GPU and power draw are not part of the basic\n"
        "                 collector; no privileged helper or sudo powermetrics is used"
    )
    return 0


def _doctor_docker(resolved: object, paths: Paths) -> int:
    print("\nDocker contexts (listing only — no container is started or changed):")
    from .core.capabilities import CommandRunner
    from .features.docker_local.contexts import list_docker_contexts, resolve_docker_binary

    binary = resolve_docker_binary()
    if binary is None:
        print("  docker CLI not found on PATH; the Docker source stays unavailable")
        return 1
    print(f"  cli            {binary}")
    runner = CommandRunner(concurrency=1)

    async def run() -> int:
        try:
            contexts, note = await list_docker_contexts(runner)
            if note:
                print(f"  note           {note}")
            for entry in contexts:
                marker = "local " if entry.is_local else "REMOTE"
                current = " *" if entry.current else "  "
                print(f"  {marker}{current} {entry.name:<16} {entry.endpoint}")
            print(
                "\n  Pin one local context in the config, for example:\n"
                '    [sources.docker.settings]\n    context = "orbstack"\n'
                "  Remote (ssh://) endpoints are refused, and the engine is never started."
            )
            return 0
        finally:
            await runner.aclose()

    return asyncio.run(run())


async def _doctor_network(resolved: object) -> int:
    import httpx

    from .core.capabilities import HttpReader, HttpScope

    print("\nPublic probes (bounded, read-only, no credentials):")
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0))
    http = HttpReader(client=client, scope=HttpScope.PUBLIC)
    results: list[tuple[str, str]] = []
    probes = (
        ("open-meteo", "https://api.open-meteo.com/v1/forecast?latitude=55.7&longitude=21.1"),
        ("hacker-news", "https://hacker-news.firebaseio.com/v0/topstories.json"),
        ("frankfurter", "https://api.frankfurter.dev/v2/providers/ecb/rate/pln/eur"),
    )
    for name, url in probes:
        try:
            response = await http.get_json(url)
            results.append((name, f"ok http={response.status} bytes={len(response.content)}"))
        except Exception as error:  # a probe result, not a crash
            results.append((name, f"{type(error).__name__}: {redact(error)[:80]}"))
    config = getattr(resolved, "sources", {})
    news = config.get("news")
    if news is not None:
        feeds = getattr(news.settings, "feeds", [])
        enabled = [feed for feed in feeds if getattr(feed, "enabled", False)]
        for feed in enabled[:12]:
            try:
                response = await http.get(feed.url)
                results.append(
                    (f"feed:{feed.id}", f"ok http={response.status} bytes={len(response.content)}")
                )
            except Exception as error:
                results.append((f"feed:{feed.id}", type(error).__name__))
    await http.aclose()
    for name, outcome in results:
        print(f"  {name:<22} {outcome}")
    print(
        "\n  A failure here is a fact about this network and moment, not proof that a provider\n"
        "  is down, and not evidence that its terms permit automated access."
    )
    return 0


def cmd_import(args: argparse.Namespace, paths: Paths) -> int:
    kind = args.import_kind
    source = Path(args.path).expanduser()
    if kind == "youtube":
        return _import_youtube(source, args)
    if not source.is_file():
        print(f"not a file: {source}", file=sys.stderr)
        return 1
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"could not read {source}: {error}", file=sys.stderr)
        return 1

    from .features.portfolio.importer import (
        CsvOptions,
        apply_preview,
        preview_fx,
        preview_positions,
        preview_prices,
    )

    delimiter = args.delimiter if args.delimiter != "tab" else "\t"
    options = CsvOptions(delimiter=delimiter, decimal=args.decimal, date_order=args.date_order)
    try:
        if kind == "holdings":
            preview = preview_positions(text, options=options, path=str(source))
        elif kind == "prices":
            preview = preview_prices(text, options=options, path=str(source))
        else:
            preview = preview_fx(text, options=options, path=str(source))
    except ConfigError as error:  # pragma: no cover - defensive
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"could not parse {source.name}: {error}", file=sys.stderr)
        return 1

    print(f"Preview of {source}")
    print(f"  {preview.summary()}")
    for warning in preview.warnings:
        print(f"  warning: {warning}")
    for row in preview.rejected[:10]:
        print(f"  rejected line {row.index}: {row.reason}")
    if preview.rejected:
        print(
            "  A snapshot with rejected rows will NOT replace stored holdings. "
            "Fix the file, or import only the price/rate rows."
        )
    if args.preview_only:
        print("preview only: nothing was written")
        return 0
    if not preview.ok:
        print("nothing to import", file=sys.stderr)
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "refusing to commit without confirmation; re-run with --yes in a script",
                file=sys.stderr,
            )
            return 1
        answer = input("Commit this import? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled: nothing was written")
            return 0

    async def run() -> int:
        database = await Database.open(paths.database_file)
        # Every registered feature's migrations, so any import kind works on a fresh
        # database without a hand-maintained list here.
        await database.initialize(tuple(CORE_MIGRATIONS) + _registry().migrations())
        try:
            now = datetime.now(UTC)
            results = await apply_preview(
                database,
                preview,
                at=now,
                allow_older=args.allow_older,
                allow_empty=args.allow_empty,
                account=args.account,
            )
            if not results:
                print("committed non-position rows only")
            for result in results:
                if result.skipped:
                    print(f"  {result.account}: skipped — {result.reason}")
                    continue
                print(
                    f"  {result.account}: replaced {result.replaced}, inserted "
                    f"{result.inserted}, rejected {result.rejected} "
                    f"(import {result.import_id})"
                )
            print("rollback is available through the stored import id")
        finally:
            await database.aclose()
        return 0

    return asyncio.run(run())


def _import_youtube(source: Path, args: argparse.Namespace) -> int:
    from .features.youtube.importers import (
        parse_subscriptions_csv,
        parse_watch_history_with_stats,
    )

    if source.is_dir():
        candidates = {
            "subscriptions": source / "subscriptions.csv",
            "watch_history": source / "watch-history.json",
        }
    else:
        candidates = {"single": source}
    printed = False
    for kind, path in candidates.items():
        if not path.is_file():
            continue
        printed = True
        text = path.read_text(encoding="utf-8", errors="replace")
        if "subscription" in kind or path.suffix == ".csv":
            channels, skipped, warnings = parse_subscriptions_csv(text)
            print(f"{path.name}: {len(channels)} channels, {skipped} skipped rows")
            for warning in warnings[:5]:
                print(f"  warning: {warning}")
            for channel in channels[:10]:
                print(f"  {channel.channel_id} {channel.title or ''}".rstrip())
        else:
            events, skipped, warnings = parse_watch_history_with_stats(text)
            print(f"{path.name}: {len(events)} watch events, {skipped} skipped entries")
            for warning in warnings[:5]:
                print(f"  warning: {warning}")
    if not printed:
        print(f"no supported Takeout file found under {source}", file=sys.stderr)
        return 1
    print(
        "\nPreview only. Add the resolved channel IDs to "
        "[sources.youtube.settings].channels, set favorite_channel_ids, then enable the\n"
        "source. Watch history is imported as recent viewing frequency, never as watched\n"
        "minutes."
    )
    _ = args
    return 0


# --------------------------------------------------------------------------- run


def _run_app(args: argparse.Namespace, paths: Paths) -> int:
    import asyncio

    from .app import SideboardApp, build_runtime

    async def boot() -> int:
        runtime = await build_runtime(paths=paths, offline=args.offline, demo=args.demo)
        app = SideboardApp(runtime, mouse=not args.no_mouse)
        await app.run_async()
        return 0

    return asyncio.run(boot())


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.version:
        from . import __version__

        print(f"sideboard {__version__}")
        return 0
    paths = _paths(args)
    if args.demo:
        paths = Paths(
            config_file=paths.config_file,
            data_dir=paths.data_dir,
            log_dir=paths.log_dir,
            database_file=paths.data_dir / "sideboard-demo.sqlite3",
        )
    configure_logging(paths.log_dir, console=args.verbose or False, demo=args.demo)

    try:
        if args.command == "init":
            return cmd_init(args, paths)
        if args.command == "config":
            if args.config_command == "validate":
                return cmd_config_validate(args, paths)
            _print_sub_help("config")
            return 2
        if args.command == "features":
            if args.features_command == "list":
                return cmd_features_list(args, paths)
            _print_sub_help("features")
            return 2
        if args.command == "themes":
            if args.themes_command == "list":
                return cmd_themes_list(args)
            if args.themes_command == "show":
                return cmd_themes_show(args)
            _print_sub_help("themes")
            return 2
        if args.command == "doctor":
            return cmd_doctor(args, paths)
        if args.command == "import":
            if args.import_kind is None:
                _print_sub_help("import")
                return 2
            return cmd_import(args, paths)
        return _run_app(args, paths)
    except RegistryError as error:
        # RegistryError is a ConfigError subclass, so it must be caught first.
        print("feature registry is invalid (this is a bug in Sideboard):", file=sys.stderr)
        for problem in error.problems[:20]:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    except ConfigError as error:
        print(f"configuration error: {error.message}", file=sys.stderr)
        for problem in error.problems[:20]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
