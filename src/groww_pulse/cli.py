"""Operator CLI. Output is content-free: codes, counts, dates and IDs only."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

from groww_pulse.config import load_config
from groww_pulse.errors import ErrorCode, PulseError

EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_INSUFFICIENT, EXIT_BLOCKED = 0, 1, 2, 3, 4

INVALID_INPUT = {
    ErrorCode.CONFIG_INVALID, ErrorCode.MANIFEST_INVALID, ErrorCode.FILE_UNREADABLE,
    ErrorCode.FORMAT_INVALID, ErrorCode.COLUMNS_INVALID, ErrorCode.INPUT_LIMIT_EXCEEDED,
    ErrorCode.CAPABILITIES_INVALID, ErrorCode.IMPORT_NOT_FOUND, ErrorCode.SNAPSHOT_NOT_FOUND,
    ErrorCode.FETCH_NOT_CONFIRMED,
    ErrorCode.RUN_NOT_FOUND, ErrorCode.CONFIG_CHANGED,
}
BLOCKING = {ErrorCode.CAPABILITY_UNAVAILABLE, ErrorCode.PRIVACY_MODEL_UNAVAILABLE,
            ErrorCode.COVERAGE_MISSING, ErrorCode.LLM_AUTH_FAILED, ErrorCode.FILTER_REFRESH_REQUIRED,
            ErrorCode.MCP_UNAVAILABLE, ErrorCode.PUBLICATION_CONFLICT}


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _date(value: str) -> date:
    return date.fromisoformat(value)


def cmd_config_check(args) -> int:
    from groww_pulse.ingestion.dates import reporting_window

    config = load_config(args.config)
    window = reporting_window(config.reporting, args.as_of)
    _emit({"status": "OK", "store": "google_play", "product_id": config.product.google_play_id,
           "window": window.model_dump(mode="json"), "email_mode": config.output.email_mode})
    return EXIT_OK


def cmd_contract_check(args) -> int:
    from groww_pulse.integrations.contract import check_capabilities, load_contract, load_tools_dump

    report = check_capabilities(load_contract(args.contract), load_tools_dump(args.tools))
    _emit({"status": "OK" if report.ok else ErrorCode.CAPABILITY_UNAVAILABLE.value,
           **report.model_dump(exclude={"ok"})})
    return EXIT_OK if report.ok else EXIT_BLOCKED


def cmd_fetch(args) -> int:
    from groww_pulse.ingestion.fetcher import fetch_public_reviews

    config = load_config(args.config)
    export, manifest, count = fetch_public_reviews(config, args.out, confirmed=args.confirm_public_source)
    _emit({"status": "OK", "records": count, "export": export.name, "manifest": manifest.name})
    return EXIT_OK


def _names(config, enabled: bool):
    from groww_pulse.privacy.detectors import spacy_names

    return spacy_names(config.privacy.spacy_model) if enabled else None


def cmd_import(args) -> int:
    from groww_pulse.ingestion.importer import import_export
    from groww_pulse.ingestion.manifest import load_manifest
    from groww_pulse.repository.db import connect

    config = load_config(args.config)
    manifest = load_manifest(args.manifest)
    names = _names(config, not args.no_ner)
    conn = connect(config.storage.database)
    import_id, counts = import_export(conn, config, manifest, args.file, names, args.as_of)
    _emit({"status": "OK", "import_id": import_id, "ner": not args.no_ner,
           "counts": counts.model_dump(mode="json")})
    return EXIT_OK


def cmd_snapshot(args) -> int:
    from groww_pulse.ingestion.snapshot import create_snapshot
    from groww_pulse.repository.db import connect

    config = load_config(args.config)
    conn = connect(config.storage.database)
    snap = create_snapshot(conn, config, args.import_id, args.as_of)
    _emit({"status": snap.outcome, **snap.model_dump(mode="json", exclude={"outcome"})})
    if snap.outcome == "READY":
        return EXIT_OK
    return EXIT_BLOCKED if snap.outcome == "COVERAGE_MISSING" else EXIT_INSUFFICIENT


def cmd_analyze(args) -> int:
    from groww_pulse.analysis.pipeline import run_analysis
    from groww_pulse.llm.base import Budget
    from groww_pulse.llm.factory import build_llm
    from groww_pulse.repository import store
    from groww_pulse.repository.db import connect

    config = load_config(args.config)
    conn = connect(config.storage.database)
    snapshot_id = args.snapshot_id or store.latest_snapshot_id(conn)
    budget = Budget(config.model)
    result = run_analysis(conn, config, snapshot_id, build_llm(config.model, budget), budget)
    # Quotes are sanitised review text: they go to a git-ignored artifact, not the terminal.
    out = Path("artifacts") / f"analysis-{result.analysis_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    _emit({"status": result.outcome, "reason": result.reason_code,
           "analysis_id": result.analysis_id, "eligible_weekly": result.eligible_weekly,
           "ranking": [r.model_dump(include={"name", "count", "share", "highlighted"})
                       for r in result.ranking],
           "quotes": len(result.quotes), "usage": result.usage, "artifact": str(out)})
    return EXIT_OK if result.outcome == "READY" else EXIT_INSUFFICIENT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse", description="Groww weekly review pulse agent")
    parser.add_argument("--config", type=Path, default=None, help="settings YAML (defaults built in)")
    sub = parser.add_subparsers(dest="command", required=True)

    cfg = sub.add_parser("config", help="configuration commands").add_subparsers(dest="action", required=True)
    check = cfg.add_parser("check", help="validate config and show the reporting window")
    check.add_argument("--as-of", type=_date)
    check.set_defaults(func=cmd_config_check)

    con = sub.add_parser("contract", help="Pulse MCP Server contract").add_subparsers(dest="action", required=True)
    ccheck = con.add_parser("check", help="check a captured list_tools JSON against the contract")
    ccheck.add_argument("--tools", type=Path, required=True)
    ccheck.add_argument("--contract", type=Path, default=Path("config/mcp_contract.yaml"))
    ccheck.set_defaults(func=cmd_contract_check)

    fetch = sub.add_parser("fetch", help="optional: collect public Google Play reviews into an export")
    fetch.add_argument("--out", type=Path, default=Path("raw_exports"))
    fetch.add_argument("--confirm-public-source", action="store_true",
                       help="confirm that collecting public listing reviews is permitted for your use")
    fetch.set_defaults(func=cmd_fetch)

    imp = sub.add_parser("import", help="sanitise and store a public export")
    imp.add_argument("--manifest", type=Path, required=True)
    imp.add_argument("--file", type=Path, required=True)
    imp.add_argument("--as-of", type=_date)
    imp.add_argument("--no-ner", action="store_true", help="skip spaCy name detection (tests only)")
    imp.set_defaults(func=cmd_import)

    snap = sub.add_parser("snapshot", help="freeze the history window for analysis")
    snap.add_argument("--import-id", type=UUID)
    snap.add_argument("--as-of", type=_date)
    snap.set_defaults(func=cmd_snapshot)

    ana = sub.add_parser("analyze", help="discover themes, rank the week, select verified quotes")
    ana.add_argument("--snapshot-id", type=UUID)
    ana.set_defaults(func=cmd_analyze)
    for command in ("preview", "publish"):
        run = sub.add_parser(command, help="run the checkpointed pulse workflow")
        run.add_argument("--snapshot-id", type=UUID)
        run.add_argument("--mcp-config", type=Path, default=Path("config/mcp_server.local.yaml"))
        run.set_defaults(func=cmd_workflow)
    resume = sub.add_parser("resume", help="resume a saved run")
    resume.add_argument("run_id", type=UUID)
    resume.add_argument("--mcp-config", type=Path, default=Path("config/mcp_server.local.yaml"))
    resume.set_defaults(func=cmd_workflow)
    inspect = sub.add_parser("inspect", help="show content-free run status")
    inspect.add_argument("run_id", type=UUID)
    inspect.set_defaults(func=cmd_inspect)
    direct = sub.add_parser("publish-direct", help="append the pulse to the Doc and send the email via gmail-docs-mcp")
    direct.add_argument("run_id", type=UUID)
    direct.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    direct.add_argument("--send", action="store_true", help="perform the writes (default: validate only)")
    direct.add_argument("--approve", action="store_true", help="confirm you have read artifacts/<run_id>/pulse.md")
    direct.set_defaults(func=cmd_publish_direct)
    ev = sub.add_parser("evaluate", help="score theme agreement on the labelled fictional review set")
    ev.add_argument("--dataset", type=Path, default=Path("tests/eval/labelled_reviews.jsonl"))
    ev.add_argument("--themes", type=Path, default=Path("tests/eval/reference_themes.json"))
    ev.add_argument("--out", type=Path, default=Path("artifacts/evaluation"))
    ev.set_defaults(func=cmd_evaluate)
    layout = sub.add_parser("confirm-layout", help="record that you checked the Doc fits one page")
    layout.add_argument("run_id", type=UUID)
    layout.set_defaults(func=cmd_confirm_layout)
    return parser


def cmd_publish_direct(args):
    from groww_pulse.integrations.gmail_docs import publish

    run_dir = args.artifacts / str(args.run_id)
    report = publish(run_dir, approve=args.approve, dry_run=not args.send)
    _emit({"status": "OK", **report})
    return EXIT_OK


def cmd_evaluate(args):
    from groww_pulse.evaluation.harness import evaluate, load_dataset, reference_taxonomy
    from groww_pulse.llm.base import Budget
    from groww_pulse.llm.factory import build_llm

    config = load_config(args.config)
    rows = load_dataset(args.dataset)
    taxonomy = reference_taxonomy(args.themes)
    report = evaluate(build_llm(config.model, Budget(config.model)), rows, taxonomy,
                      batch_size=config.model.classify_batch_size, effort=config.model.classify_effort)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    _emit({"status": "OK", **report})
    return EXIT_OK if report["passed"] else EXIT_FAILED


def cmd_inspect(args):
    from groww_pulse.repository.db import connect
    from groww_pulse.repository.workflow import get_run
    with connect(load_config(args.config).storage.database) as conn:
        state = get_run(conn, args.run_id)
        _emit(state)
    return EXIT_OK


def cmd_confirm_layout(args):
    from groww_pulse.integrations.publication import confirm_layout
    from groww_pulse.repository.db import connect
    with connect(load_config(args.config).storage.database) as conn:
        confirm_layout(conn, args.run_id)
    _emit({"status": "MANUALLY_CONFIRMED", "run_id": args.run_id})
    return EXIT_OK


def cmd_workflow(args):
    import os
    from contextlib import ExitStack

    from groww_pulse.integrations.mcp_client import connect_mcp, load_mcp_config
    from groww_pulse.integrations.publication import Publisher
    from groww_pulse.llm.base import Budget
    from groww_pulse.llm.factory import build_llm, model_ref
    from groww_pulse.repository import store
    from groww_pulse.repository.db import connect
    from groww_pulse.repository.workflow import get_run
    from groww_pulse.workflow.graph import execute
    from groww_pulse.workflow.nodes import Nodes

    config = load_config(args.config)
    budget = Budget(config.model)

    configured_model_ref = model_ref(config.model)

    class LazyLLM:
        model_ref = configured_model_ref
        adapter = None

        def complete_json(self, **kwargs):
            if self.adapter is None:
                self.adapter = build_llm(config.model, budget)
            return self.adapter.complete_json(**kwargs)

    with ExitStack() as stack:
        conn = connect(config.storage.database)
        stack.callback(conn.close)
        run_id = getattr(args, "run_id", None)
        saved = get_run(conn, run_id) if run_id else None
        mode = saved["mode"] if saved else args.command
        snapshot_id = saved["snapshot_id"] if saved else (args.snapshot_id or store.latest_snapshot_id(conn))
        class LazyPublisher:
            adapter = None

            def __getattr__(self, name):
                if self.adapter is None:
                    if not args.mcp_config.is_file():
                        raise PulseError(ErrorCode.MCP_UNAVAILABLE)
                    mcp_config = load_mcp_config(args.mcp_config)
                    client = stack.enter_context(connect_mcp(mcp_config))
                    self.adapter = Publisher(conn, client, os.environ.get(mcp_config.recipient_env, ""),
                                             config.execution.safe_mcp_attempts)
                return getattr(self.adapter, name)

        name_detector = None

        def lazy_names(text):
            nonlocal name_detector
            if name_detector is None:
                name_detector = _names(config, True)
            return name_detector(text)

        publisher = LazyPublisher() if mode == "publish" else None
        nodes = Nodes(conn, config, LazyLLM(), budget, lazy_names, publisher)
        result = execute(nodes, snapshot_id=snapshot_id, mode=mode, run_id=run_id)
        _emit(result)
        return {"PREVIEW_READY": EXIT_OK, "COMPLETE": EXIT_OK, "BLOCKED": EXIT_BLOCKED,
                "INSUFFICIENT_EVIDENCE": EXIT_INSUFFICIENT}.get(result["status"], EXIT_FAILED)


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path(".env"), override=False)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PulseError as exc:
        _emit({"status": "ERROR", "code": exc.code.value})
        if exc.code in INVALID_INPUT:
            return EXIT_INVALID
        return EXIT_BLOCKED if exc.code in BLOCKING else EXIT_FAILED
    except Exception:  # Never print exception text: it may contain review content.
        _emit({"status": "ERROR", "code": ErrorCode.INTERNAL_ERROR.value})
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
