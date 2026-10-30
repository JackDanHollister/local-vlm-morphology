import argparse
import json
from pathlib import Path

from .store import Store
from .motion import VIEWS


def main():
    parser = argparse.ArgumentParser(description="Evidence-linked specimen observation pilot")
    parser.add_argument("--store", default="experiments/specimen-lab")
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import-image")
    imp.add_argument("specimen_id")
    imp.add_argument("image")
    imp.add_argument("--label", required=True)
    crop = commands.add_parser("crop")
    crop.add_argument("image_id")
    crop.add_argument("box", nargs=4, type=int, metavar="XYWH")
    observe = commands.add_parser("observe")
    observe.add_argument("specimen_id")
    observe.add_argument("image_ids", nargs="+")
    observe.add_argument("--model", default="gpt-6-astra")
    observe.add_argument("--next-view", action="append", default=[])
    observe.add_argument("--motion-session", help="Offer small/medium/large Y/Z views within recorded limits and travel budget")
    observe.add_argument("--timeout", type=int, default=300)
    commands.add_parser("status")
    commands.add_parser("audit")
    views=commands.add_parser("views")
    views.add_argument("--session", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("specimen_id")
    capture.add_argument("--view", default="CURRENT", choices=list(VIEWS))
    capture.add_argument("--attended", action="store_true")
    capture.add_argument("--session", required=True)
    active = commands.add_parser("next-view")
    active.add_argument("parent_run_id")
    active.add_argument("--attended", action="store_true")
    active.add_argument("--session", required=True)
    export = commands.add_parser("export")
    export.add_argument("--format", choices=["json", "jsonld", "graph"], default="jsonld")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--recorded-only", action="store_true", help="Accept saved-image analysis only")
    serve.add_argument("--view-only", action="store_true", help="Browse saved results without loading the demo controller")
    args = parser.parse_args()
    store = Store(args.store)
    if args.command == "import-image":
        store.specimen(args.specimen_id, args.label)
        result = store.add_image(args.specimen_id, Path(args.image))
    elif args.command == "crop":
        result = store.crop(args.image_id, args.box)
    elif args.command == "observe":
        from .observer import observe
        if args.motion_session:
            if args.next_view:
                parser.error("Use either --motion-session or explicit --next-view values")
            from .hardware import movement_choices
            args.next_view=movement_choices(store,args.motion_session)["views"]
        result = observe(store, args.specimen_id, args.image_ids, args.model,
                         next_views=args.next_view, timeout=args.timeout)
    elif args.command == "capture":
        from .hardware import capture
        result = capture(store, args.specimen_id, args.view, attended=args.attended, session_id=args.session)
    elif args.command == "next-view":
        from .active import next_view
        result = next_view(store, args.parent_run_id, attended=args.attended, session_id=args.session)
    elif args.command == "serve":
        from .server import serve
        serve(store, args.port, allow_live=not (args.recorded_only or args.view_only), view_only=args.view_only)
        return
    elif args.command == "export":
        result = {"json": store.snapshot, "jsonld": store.jsonld, "graph": store.graph}[args.format]()
    elif args.command == "audit":
        result = store.audit()
    elif args.command == "views":
        from .hardware import movement_choices
        result = movement_choices(store,args.session)
    else:
        result = [{"id": r["id"], "state": r["state"], "model": r["model"], "errors": r.get("errors", [])} for r in store.snapshot()["runs"]]
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    if args.command in {"observe", "next-view"} and result["state"] not in {"usable", "finished"}:
        raise SystemExit(1)
    if args.command == "audit" and not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
