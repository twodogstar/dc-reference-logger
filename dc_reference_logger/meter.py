"""HP 34401A setup, a PyVISA session, and a deterministic simulator."""

from contextlib import contextmanager, nullcontext
from itertools import cycle
import math
import re

GPIB_RESOURCE = re.compile(r"GPIB([0-9]+)::([0-9]|[12][0-9]|30)::INSTR", re.IGNORECASE)
MANUFACTURERS = {"HEWLETT-PACKARD", "AGILENT TECHNOLOGIES", "KEYSIGHT TECHNOLOGIES"}


class MeterError(RuntimeError):
    """An instrument response cannot be used for this run."""


def is_34401a(identity):
    fields = [field.strip().upper() for field in identity.split(",")]
    return len(fields) == 4 and fields[0] in MANUFACTURERS and fields[1] == "34401A"


def read_voltage(raw):
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
        raise MeterError(f"Invalid voltage response: {raw!r}")
    voltage = float(raw)
    if not math.isfinite(voltage) or abs(voltage) > 12:
        raise MeterError(f"Invalid or overrange reading for the 10 V range: {raw!r}")
    return voltage


def check_error_queue(meter):
    errors = []
    for _ in range(20):
        response = meter.query("SYST:ERR?").strip()
        code, comma, _ = response.partition(",")
        if not comma or not re.fullmatch(r"[+-]?\d+", code.strip()):
            raise MeterError(f"Invalid meter error response: {response!r}")
        if int(code) == 0:
            break
        errors.append(response)
    if errors:
        raise MeterError("Meter configuration error: " + "; ".join(errors))


def configure(meter, settings, readback):
    # CONFigure sets the measurement and presets its trigger and math state.
    meter.write("*CLS")
    meter.write(f"CONF:VOLT:DC {settings['range_v']}")
    meter.write(f"VOLT:DC:NPLC {settings['nplc']}")
    meter.write("ZERO:AUTO ON")
    meter.write("INP:IMP:AUTO ON")
    check_error_queue(meter)
    queries = {
        "function": "FUNC?",
        "range_v": "VOLT:DC:RANG?",
        "autorange": "VOLT:DC:RANG:AUTO?",
        "nplc": "VOLT:DC:NPLC?",
        "autozero": "ZERO:AUTO?",
        "auto_input_impedance": "INP:IMP:AUTO?",
        "trigger_source": "TRIG:SOUR?",
        "auto_trigger_delay": "TRIG:DEL:AUTO?",
        "sample_count": "SAMP:COUN?",
        "trigger_count": "TRIG:COUN?",
        "math_enabled": "CALC:STAT?",
    }
    for key, query in queries.items():
        readback[key] = meter.query(query).strip()
    check_error_queue(meter)

    def matches(key, actual):
        expected = settings[key]
        if isinstance(expected, (int, float)):
            try:
                return math.isclose(float(actual), expected, rel_tol=1e-9)
            except ValueError:
                return False
        actual = actual.strip('"').upper()
        if key == "function":
            return actual in {"VOLT", "VOLT:DC"}
        return actual == expected

    mismatches = [
        f"{key} expected {settings[key]!r}, got {actual!r}"
        for key, actual in readback.items() if not matches(key, actual)
    ]
    if mismatches:
        raise MeterError("Settings did not match: " + "; ".join(mismatches))


@contextmanager
def visa_meter(resource, backend, timeout_ms, cleanup_errors):
    try:
        import pyvisa
        from pyvisa.constants import RENLineOperation
    except ImportError as exc:
        raise MeterError("PyVISA is missing. See README.md for the hardware setup.") from exc

    manager = None
    meter = None
    try:
        manager = pyvisa.ResourceManager(backend)
        meter = manager.open_resource(resource)
        meter.timeout = timeout_ms
        meter.write_termination = "\n"
        meter.read_termination = None
        meter.send_end = True
        yield meter
    finally:
        cleanup = []
        if meter is not None:
            cleanup.extend([
                ("go local", lambda: go_local(manager, meter, resource, backend, timeout_ms, RENLineOperation.address_gtl)),
                ("close instrument session", meter.close),
            ])
        if manager is not None:
            cleanup.append(("close resource manager", manager.close))
        for action, attempt in cleanup:
            try:
                attempt()
            except Exception as exc:
                cleanup_errors.append(f"{action}: {exc}")


def go_local(manager, meter, resource, backend, timeout_ms, address_gtl):
    if backend != "@py":
        meter.control_ren(address_gtl)
        return

    # Send addressed command bytes directly. PyVISA-py 0.8.1's address_gtl
    # implementation sends the ASCII string b"0x1" instead of the GTL byte.
    board, address = GPIB_RESOURCE.fullmatch(resource).groups()
    unlisten, listen, gtl = 0x3F, 0x20 + int(address), 0x01
    commands = bytes([unlisten, listen, gtl, unlisten])
    with manager.open_resource(f"GPIB{board}::INTFC") as bus:
        bus.timeout = timeout_ms
        # PyVISA-py 0.8.1 forwards the driver's return value as the count.
        # That value can be None or an ibsta status word, so use VISA status.
        _, status = bus.send_command(commands)
        if status < 0:
            raise MeterError(f"Addressed Go To Local failed (VISA status {status}).")


class SimulatedMeter:
    def __init__(self):
        self.nplc = "100"
        self.samples = cycle(["5.00000000", "5.00000010", "4.99999970", "4.99999980"])

    def write(self, command):
        if command.startswith("VOLT:DC:NPLC "):
            self.nplc = command.split()[-1]

    def query(self, command):
        if command == "READ?":
            return next(self.samples)
        return {
            "*IDN?": "HEWLETT-PACKARD,34401A,SIMULATED,0",
            "SYST:ERR?": '+0,"No error"',
            "FUNC?": '"VOLT:DC"',
            "VOLT:DC:RANG?": "10",
            "VOLT:DC:RANG:AUTO?": "0",
            "VOLT:DC:NPLC?": self.nplc,
            "ZERO:AUTO?": "1",
            "INP:IMP:AUTO?": "1",
            "TRIG:SOUR?": "IMM",
            "TRIG:DEL:AUTO?": "1",
            "SAMP:COUN?": "1",
            "TRIG:COUN?": "1",
            "CALC:STAT?": "0",
        }[command]


def open_meter(args, cleanup_errors):
    if args.simulate:
        return nullcontext(SimulatedMeter())
    return visa_meter(args.resource, args.backend, args.timeout_ms, cleanup_errors)
