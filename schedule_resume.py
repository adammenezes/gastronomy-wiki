"""
schedule_resume.py — wait until a time, then run a command or open Claude Code.

Usage:
  python schedule_resume.py 14:30                          # run default command at 2:30pm today
  python schedule_resume.py 14:30 --cmd "python agent/compile.py"
  python schedule_resume.py 14:30 --claude "continue the benchmark work on cooking-brain"
  python schedule_resume.py 14:30 --claude "continue the benchmark work" --print   # non-interactive
  python schedule_resume.py +2h                            # 2 hours from now
  python schedule_resume.py +90m                           # 90 minutes from now
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta


def parse_target_time(spec: str) -> datetime:
    now = datetime.now()

    # Relative: +2h, +90m, +30s
    if spec.startswith("+"):
        rest = spec[1:].strip().lower()
        if rest.endswith("h"):
            delta = timedelta(hours=float(rest[:-1]))
        elif rest.endswith("m"):
            delta = timedelta(minutes=float(rest[:-1]))
        elif rest.endswith("s"):
            delta = timedelta(seconds=float(rest[:-1]))
        else:
            raise ValueError(f"Unknown relative format: {spec}. Use +2h, +90m, +30s")
        return now + delta

    # Absolute: HH:MM or HH:MM:SS
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(spec, fmt)
            target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            if target <= now:
                target += timedelta(days=1)   # already passed today — schedule tomorrow
            return target
        except ValueError:
            continue

    raise ValueError(f"Could not parse time: '{spec}'. Use HH:MM or +2h or +90m.")


def countdown(target: datetime):
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        m, s = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        print(f"\r  Waiting... {h:02d}:{m:02d}:{s:02d} remaining  (fires at {target:%H:%M:%S})", end="", flush=True)
        time.sleep(1)
    print(f"\r  Time reached: {datetime.now():%H:%M:%S}                              ")


def main():
    parser = argparse.ArgumentParser(description="Schedule a command to run at a specific time.")
    parser.add_argument("time", help="Target time: HH:MM or +2h or +90m")
    parser.add_argument("--cmd",    default=None,
                        help="Shell command to run (e.g. 'python agent/compile.py')")
    parser.add_argument("--claude", default=None,
                        help="Prompt to send to Claude Code CLI")
    parser.add_argument("--print",  dest="print_mode", action="store_true",
                        help="Use claude --print (non-interactive, prints response and exits)")
    args = parser.parse_args()

    try:
        target = parse_target_time(args.time)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Scheduled for: {target:%Y-%m-%d %H:%M:%S}")

    if args.claude:
        mode = "--print" if args.print_mode else ""
        print(f"Will run: claude {mode} \"{args.claude}\"")
    elif args.cmd:
        print(f"Will run: {args.cmd}")
    else:
        print("Will run: python agent/compile.py  (default)")

    print("Press Ctrl+C to cancel.\n")

    try:
        countdown(target)
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)

    # Fire
    if args.claude:
        if args.print_mode:
            cmd = ["claude", "--print", args.claude]
        else:
            # Opens Claude Code interactively with the prompt pre-filled
            cmd = ["claude", args.claude]
        print(f"Running: {' '.join(cmd)}")
        subprocess.run(cmd)

    elif args.cmd:
        print(f"Running: {args.cmd}")
        subprocess.run(args.cmd, shell=True)

    else:
        # Default: run the pipeline
        cmd = [sys.executable, "agent/compile.py"]
        print(f"Running: {' '.join(cmd)}")
        subprocess.run(cmd)


if __name__ == "__main__":
    main()
