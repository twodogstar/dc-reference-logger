"""Command-line acquisition and durable run records."""

import argparse
from contextlib import ExitStack, contextmanager
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time
from uuid import uuid4

from . import __version__
from .meter import GPIB_RESOURCE, MeterError, configure, is_34401a, open_meter, read_voltage

COLUMNS = ("timestamp", "elapsed_s", "volts", "delta_ppm")
EVENT_COLUMNS = ("timestamp", "elapsed_s", "phase", "error_type", "message", "consecutive_failures")
RETRY_DELAY_S = 1.0


def positive_duration(value):
    match = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)([smhd]?)", value.strip().lower())
    if match:
        amount, unit = match.groups()
        seconds = float(amount) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    raise argparse.ArgumentTypeError("use a positive duration such as 15s, 30m, or 12h")


def nonnegative_interval(value):
    try:
        seconds = float(value)
        if math.isfinite(seconds) and (seconds == 0 or seconds >= 0.001):
            return seconds
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("interval must be 0 or a finite number of seconds at least 0.001")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Log a DC voltage reference with an HP 34401A over GPIB.")
    parser.add_argument("--version", action="version", version=__version__)
    input_source = parser.add_mutually_exclusive_group(required=True)
    input_source.add_argument("--simulate", action="store_true", help="use a deterministic simulated meter")
    input_source.add_argument("--resource", help="GPIB instrument resource, e.g. GPIB0::22::INSTR")
    parser.add_argument("--reference", help="optional reference label stored in the run record")
    parser.add_argument("--backend", default="@py", help="PyVISA backend (default %(default)s)")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="VISA I/O timeout (default %(default)s)")
    parser.add_argument(
        "--max-consecutive-failures", type=int, default=5,
        help="stop after this many consecutive failed attempts (default %(default)s)",
    )
    parser.add_argument("--nplc", type=float, choices=(0.02, 0.2, 1, 10, 100), default=100)
    parser.add_argument("--interval", type=nonnegative_interval, default=0, help="seconds between reading starts, or 0 for back-to-back hardware reads")
    parser.add_argument("--duration", type=positive_duration, help="acquisition duration, e.g. 15s or 12h")
    parser.add_argument("--out", type=Path, help="new CSV path (default runs/dcv_<timestamp>_<id>.csv)")
    parser.add_argument("--note", default="", help="short setup or environmental note")
    args = parser.parse_args(argv)
    if args.resource:
        if not GPIB_RESOURCE.fullmatch(args.resource):
            parser.error("--resource must be a primary-address GPIB instrument, e.g. GPIB0::22::INSTR")
        args.resource = args.resource.upper()
    if not 1 <= args.timeout_ms <= 2_147_483_647:
        parser.error("--timeout-ms must be between 1 and 2147483647")
    if args.max_consecutive_failures < 1:
        parser.error("--max-consecutive-failures must be at least 1")
    if args.out and args.out.suffix.lower() != ".csv":
        parser.error("--out must end in .csv")
    if args.simulate and not args.interval:
        args.interval = 1.0
    return args


def wall_time():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def source_revision():
    # Resolve Git from the source tree, without borrowing a parent repository
    # or a repository that happens to be the user's working directory.
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=12"],
            capture_output=True, text=True, timeout=2, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def save_metadata(path, metadata):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(metadata, output, indent=2, allow_nan=False)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def reserve_outputs(csv_path, metadata_path, events_path, metadata):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    # Claim each output exclusively, rolling back only paths this run created.
    paths = (metadata_path, csv_path, events_path)
    with ExitStack() as files:
        streams = []
        try:
            for path in paths:
                streams.append(files.enter_context(path.open("x", encoding="utf-8", newline="")))
        except OSError:
            files.close()
            for path in paths[:len(streams)]:
                path.unlink()
            raise

        metadata_file, samples, events = streams
        metadata_file.close()
        save_metadata(metadata_path, metadata)
        sample_writer = csv.writer(samples)
        event_writer = csv.writer(events)
        sample_writer.writerow(COLUMNS)
        event_writer.writerow(EVENT_COLUMNS)
        samples.flush()
        events.flush()
        yield samples, sample_writer, events, event_writer


class Stats:
    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf

    def add(self, volts):
        self.count += 1
        difference = volts - self.mean
        self.mean += difference / self.count
        self.m2 += difference * (volts - self.mean)
        self.minimum = min(volts, self.minimum)
        self.maximum = max(volts, self.maximum)

    def summary(self):
        if not self.count:
            return None
        return {
            "mean_volts": self.mean,
            "min_volts": self.minimum,
            "max_volts": self.maximum,
            "peak_to_peak_volts": self.maximum - self.minimum,
            "sample_stddev_volts": math.sqrt(self.m2 / (self.count - 1)) if self.count > 1 else None,
        }


def acquire(meter, transport_errors, args, stop, metadata, outputs, stats):
    samples, sample_writer, events, event_writer = outputs
    retryable = (MeterError, OSError, UnicodeError) + transport_errors
    errors = metadata["acquisition_errors"]
    first = None
    start = time.monotonic()
    deadline = math.inf if args.duration is None else start + args.duration
    target = start
    while not stop["requested"]:
        now = time.monotonic()
        if now >= deadline:
            break
        if now < target:
            time.sleep(min(0.2, target - now, deadline - now))
            continue

        phase = "device_clear"
        try:
            if errors["consecutive"]:
                # An earlier READ? may still have a response queued.
                meter.clear()
                if stop["requested"] or time.monotonic() >= deadline:
                    break
            phase = "read"
            raw = meter.query("READ?").strip()
            volts = read_voltage(raw)
        except retryable as exc:
            finished = time.monotonic()
            errors["total"] += 1
            errors[phase] += 1
            errors["consecutive"] += 1
            streak = errors["consecutive"]
            errors["max_consecutive"] = max(errors["max_consecutive"], streak)
            message = f"{type(exc).__name__}: {exc}"
            event_writer.writerow((
                wall_time(), f"{finished - start:.6f}", phase,
                type(exc).__name__, str(exc), streak,
            ))
            events.flush()
            print(
                f"Acquisition warning ({streak}/{args.max_consecutive_failures}, {phase}): {message}",
                file=sys.stderr, flush=True,
            )
            if streak >= args.max_consecutive_failures and not stop["requested"]:
                raise MeterError(f"Reached {streak} consecutive acquisition failures: {message}") from exc
        else:
            finished = time.monotonic()
            errors["consecutive"] = 0
            timestamp = wall_time()
            if first is None:
                first = volts
            ppm = (volts - first) / first * 1e6 if first else None
            ppm_field = f"{ppm:+.3f}" if ppm is not None and math.isfinite(ppm) else ""
            sample_writer.writerow((timestamp, f"{finished - start:.6f}", raw, ppm_field))
            samples.flush()
            stats.add(volts)
            ppm_label = f"{ppm_field} ppm" if ppm_field else "ppm unavailable"
            print(f"{timestamp}  #{stats.count:<6} {volts:.8f} V  {ppm_label}", flush=True)

        if args.interval:
            target += args.interval
            # Skip nominal starts that elapsed while the meter was busy.
            if target < finished:
                missed = math.ceil((min(finished, deadline) - target) / args.interval)
                if missed > 0:
                    metadata["missed_starts"] += missed
                    target += missed * args.interval
        elif errors["consecutive"]:
            target = finished + RETRY_DELAY_S
    return "operator_stop" if stop["requested"] else "duration_elapsed"


def run(args, stop):
    run_id = uuid4().hex
    csv_path = args.out or Path("runs") / f"dcv_{datetime.now():%Y%m%d_%H%M%S}_{run_id[:8]}.csv"
    metadata_path = csv_path.with_suffix(".json")
    events_path = csv_path.with_suffix(".events.csv")
    stats = Stats()
    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "software_version": __version__,
        "source_revision": source_revision(),
        "mode": "simulation" if args.simulate else "hardware",
        "resource": "simulated" if args.simulate else args.resource,
        "backend": None if args.simulate else args.backend,
        "instrument_id": None,
        "reference": args.reference,
        "setup_note": args.note,
        "started_at": wall_time(),
        "finished_at": None,
        "sample_csv": csv_path.name,
        "sample_count": 0,
        "events_csv": events_path.name,
        "max_consecutive_failures": args.max_consecutive_failures,
        "acquisition_errors": {"total": 0, "read": 0, "device_clear": 0, "consecutive": 0, "max_consecutive": 0},
        "missed_starts": 0,
        "duration_s": args.duration,
        "interval_s": args.interval,
        "timeout_ms": None if args.simulate else args.timeout_ms,
        "requested_settings": {
            "function": "VOLT:DC", "range_v": 10, "autorange": 0, "nplc": args.nplc,
            "autozero": 1, "auto_input_impedance": 1, "trigger_source": "IMM",
            "auto_trigger_delay": 1, "sample_count": 1, "trigger_count": 1, "math_enabled": 0,
        },
        "settings_readback": {},
        "configuration_verified": False,
        "termination": "running",
        "error": None,
        "cleanup_warnings": [],
        "statistics": None,
    }
    with reserve_outputs(csv_path, metadata_path, events_path, metadata) as outputs:
        print(f"CSV: {csv_path}\nRun record: {metadata_path}\nEvents: {events_path}", flush=True)
        try:
            with open_meter(args, metadata["cleanup_warnings"]) as (meter, transport_errors):
                identity = meter.query("*IDN?").strip()
                metadata["instrument_id"] = identity
                if not is_34401a(identity):
                    raise MeterError(f"Expected an HP 34401A, received {identity!r}.")
                if not stop["requested"]:
                    configure(meter, metadata["requested_settings"], metadata["settings_readback"])
                    metadata["configuration_verified"] = True
                save_metadata(metadata_path, metadata)
                metadata["termination"] = acquire(meter, transport_errors, args, stop, metadata, outputs, stats)
        except KeyboardInterrupt:
            metadata["termination"] = "operator_stop"
        except Exception as exc:
            metadata["termination"] = "failed"
            metadata["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            metadata["finished_at"] = wall_time()
            metadata["sample_count"] = stats.count
            metadata["statistics"] = stats.summary()
            save_metadata(metadata_path, metadata)

    if metadata["error"]:
        print(metadata["error"], file=sys.stderr)
    for warning in metadata["cleanup_warnings"]:
        print(f"Cleanup warning: {warning}", file=sys.stderr)
    print(
        f"{metadata['termination']}: {stats.count} samples saved, "
        f"{metadata['acquisition_errors']['total']} acquisition errors."
    )
    return 1 if metadata["termination"] == "failed" else 0


def main(argv=None):
    args = parse_args(argv)
    stop = {"requested": False}

    def request_stop(_signum, _frame):
        stop["requested"] = True

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return run(args, stop)
    except OSError as exc:
        print(f"Could not write run files: {exc}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
