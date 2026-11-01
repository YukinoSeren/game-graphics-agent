import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from uuid import uuid4

from .providers import ExaSearch, ModelClient, ProviderError, PublicReader
from .workflow import Collector


def main(argv=None):
    parser = argparse.ArgumentParser(description="PC game graphics evidence collector (contract v0.1)")
    parser.add_argument("--game", help="canonical game name, e.g. Forza Horizon 6")
    parser.add_argument("--alias", action="append", default=[], help="explicit alternate game name")
    parser.add_argument("--gpu", help="optional GPU context; no FPS target is required")
    parser.add_argument("--resolution", help="optional resolution context")
    parser.add_argument("--build", help="optional raw game build identifier")
    parser.add_argument("--mode", choices=["seeded", "discover"], default="discover")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--model", help="override COLLECTOR_MODEL")
    parser.add_argument("--read-only", action="store_true", help="debug search/read only; always reports incomplete extraction")
    parser.add_argument("--check-model", action="store_true", help="check model endpoint credentials without generating text")
    parser.add_argument("--output", type=Path, help="new or empty run directory")
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-searches", type=int, default=12)
    parser.add_argument("--followup-rounds", type=int, default=2)
    parser.add_argument("--max-model-calls", type=int, default=16)
    parser.add_argument("--max-seconds", type=float)
    args = parser.parse_args(argv)
    try:
        if args.check_model:
            print(json.dumps(ModelClient(model=args.model).check(), ensure_ascii=False, indent=2))
            return 0
        if not args.game:
            raise ValueError("--game is required")
        if args.mode == "seeded" and not args.url:
            raise ValueError("seeded mode requires --url")
        model = None if args.read_only else ModelClient(model=args.model)
        output = args.output or Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
        request = {"game_name": args.game, "game_aliases": args.alias, "mode": args.mode, "seed_urls": args.url, "platform_scope": "Windows PC", "budget": {"max_urls": args.max_pages, "max_searches": args.max_searches, "max_followup_rounds": args.followup_rounds, "max_model_calls": args.max_model_calls, "max_seconds": args.max_seconds}}
        if args.gpu:
            request["gpu"] = args.gpu
        if args.resolution:
            request["resolution"] = args.resolution
        if args.build:
            request["game_build"] = args.build
        result = Collector(ExaSearch(), PublicReader(), model, progress=lambda s: print(s, file=sys.stderr, flush=True)).run(request, output)
        print(json.dumps({"status": result["status"], "stop_reason": result["stop_reason"], "calls": result["calls"], "output": str(output.resolve())}, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed_scope" else 2
    except (ProviderError, ValueError, OSError) as error:
        print("Collector: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
