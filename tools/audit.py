#!/usr/bin/env python3
"""Audit every channel source and report which ones work.

Standard library only, so it runs anywhere without the server's virtualenv:

    tools/audit.py                                   # audit everything
    tools/audit.py --url http://tv.lan:8409 --token XYZ
    tools/audit.py --channel bbc1 --duration 15      # one channel, longer test
    tools/audit.py --failed-only                     # just the broken ones
    tools/audit.py --json > audit.json               # for scripting

Exit code is 0 when every source passed, 1 when any failed, 2 on a usage or
transport error - so it drops straight into cron or a CI check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, GREY = "\033[32m", "\033[31m", "\033[33m", "\033[90m"

MARKS = {
    "ok": (GREEN, "PASS"),
    "failed": (RED, "FAIL"),
    "skipped": (YELLOW, "SKIP"),
    "pending": (GREY, "...."),
    "testing": (GREY, "····"),
}


class Api:
    def __init__(self, base: str, token: str = "", timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, params: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        request = urllib.request.Request(url, method=method)
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body
            raise SystemExit(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach {self.base}: {exc.reason}") from exc


def human_rate(bps: int) -> str:
    if not bps:
        return "-"
    if bps >= 1_000_000:
        return f"{bps / 1e6:.1f}Mb/s"
    return f"{bps // 1000}kb/s"


def render(results: list[dict], colour: bool, show_all: bool) -> str:
    rows = results if show_all else [r for r in results if r["status"] != "ok"]
    if not rows:
        return ""

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if colour else text

    def channel_label(r: dict) -> str:
        number = f"{r['channel_number']:>4} " if r.get("channel_number") is not None else "     "
        return number + r["channel_name"]

    widths = {
        "channel": max(7, *(len(channel_label(r)) for r in rows)),
        "source": max(6, *(len(r["source_name"] or r["source_id"]) for r in rows)),
        "video": max(5, *(len(r["video"]) for r in rows)),
    }
    lines = []
    header = (
        f"{'':4}  {'CHANNEL'.ljust(widths['channel'])}  "
        f"{'SOURCE'.ljust(widths['source'])}  "
        f"{'VIDEO'.ljust(widths['video'])}  {'RATE':>9}  DETAIL"
    )
    lines.append(paint(header, BOLD) if colour else header)

    for r in rows:
        code, mark = MARKS.get(r["status"], (GREY, r["status"][:4].upper()))
        detail = r["error"] or r["audio"] or ""
        lines.append(
            f"{paint(mark, code)}  "
            f"{channel_label(r).ljust(widths['channel'])}  "
            f"{(r['source_name'] or r['source_id']).ljust(widths['source'])}  "
            f"{r['video'].ljust(widths['video'])}  "
            f"{human_rate(r['bitrate_bps']):>9}  {detail[:70]}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("STREAMS_MANAGER_URL", "http://127.0.0.1:8409"))
    parser.add_argument("--token", default=os.environ.get("STREAMS_MANAGER_TOKEN", ""))
    parser.add_argument("--channel", help="audit a single channel by id")
    parser.add_argument("--duration", type=float, default=8.0, help="seconds per source (default 8)")
    parser.add_argument("--concurrency", type=int, default=3, help="sources tested at once (default 3)")
    parser.add_argument("--include-disabled", action="store_true", help="also test disabled sources")
    parser.add_argument("--failed-only", action="store_true", help="only print sources that did not pass")
    parser.add_argument("--json", action="store_true", help="emit the raw result JSON")
    parser.add_argument("--no-colour", action="store_true")
    parser.add_argument("--results", action="store_true",
                        help="print the last completed audit without starting a new one")
    args = parser.parse_args()

    api = Api(args.url, args.token)
    colour = sys.stdout.isatty() and not args.no_colour

    if args.results:
        state = api.call("GET", "/api/audit")
        if not state["results"]:
            print("no audit has been run yet", file=sys.stderr)
            return 2
    else:
        state = api.call(
            "POST",
            "/api/audit",
            {
                "duration": args.duration,
                "concurrency": args.concurrency,
                "channel": args.channel,
                "include_disabled": args.include_disabled,
            },
        )
        total = state["total"]
        if not total:
            print("no sources to audit", file=sys.stderr)
            return 2
        if not args.json:
            print(f"Auditing {total} source(s), {args.duration:g}s each…", file=sys.stderr)

        try:
            while True:
                time.sleep(2)
                state = api.call("GET", "/api/audit")
                if not args.json and sys.stderr.isatty():
                    print(
                        f"\r  {state['completed']}/{state['total']} checked"
                        f"  ({state['counts'].get('ok', 0)} ok,"
                        f" {state['counts'].get('failed', 0)} failed)   ",
                        end="", file=sys.stderr, flush=True,
                    )
                if not state["running"]:
                    break
        except KeyboardInterrupt:
            print("\nstopping…", file=sys.stderr)
            state = api.call("POST", "/api/audit/stop")
        if not args.json and sys.stderr.isatty():
            print(file=sys.stderr)

    if args.json:
        json.dump(state, sys.stdout, indent=2)
        print()
    else:
        table = render(state["results"], colour, show_all=not args.failed_only)
        if table:
            print(table)
        counts = state["counts"]
        print(
            f"\n{counts.get('ok', 0)} ok, {counts.get('failed', 0)} failed, "
            f"{counts.get('skipped', 0)} skipped, in {state['elapsed']:.0f}s"
        )
        if counts.get("failed"):
            sys.stdout.flush()
            print("\nRe-test one with:  tools/audit.py --channel <id>", file=sys.stderr)

    return 1 if state["counts"].get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
