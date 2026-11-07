from contextlib import contextmanager
import csv
from datetime import datetime
import json
import signal

import pytest

from dc_reference_logger import logger
from dc_reference_logger.meter import MeterError, SimulatedMeter, go_local


class Meter(SimulatedMeter):
    def __init__(self, samples=(), replies=None):
        super().__init__()
        self.samples = iter(samples)
        self.replies = replies or {}
        self.writes = []
        self.closed = False
        self.clears = 0

    def clear(self):
        self.clears += 1

    def write(self, command):
        self.writes.append(command)
        super().write(command)

    def query(self, command):
        if command in self.replies:
            return self.replies[command]
        if command == "READ?":
            result = next(self.samples)
            if isinstance(result, BaseException):
                raise result
            if result == "stop":
                signal.raise_signal(signal.SIGINT)
                return "5.00"
            return result
        return super().query(command)


def install_fake(monkeypatch, meter, transport_errors=()):
    @contextmanager
    def fake_open(_args, _warnings):
        try:
            yield meter, transport_errors
        finally:
            meter.closed = True

    monkeypatch.setattr(logger, "open_meter", fake_open)


def output(path):
    with path.open(newline="") as data:
        rows = list(csv.DictReader(data))
    return rows, json.loads(path.with_suffix(".json").read_text())


def read_events(path):
    with path.with_suffix(".events.csv").open(newline="") as data:
        return list(csv.DictReader(data))


def test_simulated_timed_run(tmp_path):
    path = tmp_path / "new.csv"
    assert logger.main(["--simulate", "--duration", ".03s",
                        "--interval", ".01", "--out", str(path), "--note", "Setup A", "--reference", "Example 5 V standard"]) == 0
    rows, record = output(path)
    assert rows
    assert tuple(rows[0]) == logger.COLUMNS
    assert datetime.fromisoformat(rows[0]["timestamp"]).utcoffset() is not None
    assert record["sample_count"] == len(rows)
    assert record["termination"] == "duration_elapsed"
    assert record["mode"] == "simulation"
    assert record["setup_note"] == "Setup A"
    assert record["reference"] == "Example 5 V standard"
    assert record["configuration_verified"]
    assert record["acquisition_errors"]["total"] == 0
    assert read_events(path) == []


@pytest.mark.parametrize("existing", [".csv", ".json", ".events.csv"])
def test_never_overwrite_output(monkeypatch, tmp_path, existing):
    path = tmp_path / "run.csv"
    old = path.with_suffix(existing)
    old.write_text("original")

    def unexpected_open(*_):
        pytest.fail("Existing output should be caught before opening the meter.")

    monkeypatch.setattr(logger, "open_meter", unexpected_open)
    assert logger.main(["--resource", "GPIB0::22::INSTR", "--out", str(path)]) == 1
    assert old.read_text() == "original"
    assert list(tmp_path.iterdir()) == [old]


def test_wrong_meter_receives_no_configuration(monkeypatch, tmp_path):
    meter = Meter(replies={"*IDN?": "FLUKE,45,0,1"})
    install_fake(monkeypatch, meter)
    path = tmp_path / "run.csv"
    assert logger.main(["--resource", "GPIB0::22::INSTR", "--out", str(path)]) == 1
    rows, record = output(path)
    assert rows == []
    assert record["instrument_id"] == "FLUKE,45,0,1"
    assert record["termination"] == "failed"
    assert meter.writes == []
    assert meter.closed


@pytest.mark.parametrize("replies,error", [
    ({"VOLT:DC:NPLC?": "1"}, "Settings did not match"),
    ({"SYST:ERR?": '-113,"Undefined header"'}, "Meter configuration error"),
])
def test_configuration_failure(monkeypatch, tmp_path, replies, error):
    meter = Meter(replies=replies)
    install_fake(monkeypatch, meter)
    path = tmp_path / "run.csv"
    assert logger.main(["--resource", "GPIB0::22::INSTR", "--out", str(path)]) == 1
    rows, record = output(path)
    assert rows == []
    assert not record["configuration_verified"]
    assert error in record["error"]
    assert meter.closed


class FakeVisaIOError(Exception):
    """A retryable transport error, supplied by the hardware adapter in production."""


@pytest.mark.parametrize("bad", [
    TimeoutError("VISA timeout"), FakeVisaIOError("bus error"),
    UnicodeDecodeError("ascii", b"\xff", 0, 1, "invalid byte"),
    "nan", "1e9999", "9.9e37", "1_0", "bad",
])
def test_recovers_after_a_bad_read(monkeypatch, tmp_path, bad):
    meter = Meter(["5.00", bad, "5.00000001", "stop"])
    install_fake(monkeypatch, meter, (FakeVisaIOError,))
    path = tmp_path / "run.csv"
    assert logger.main([
        "--resource", "GPIB0::22::INSTR", "--interval", ".001", "--out", str(path),
    ]) == 0
    rows, record = output(path)
    assert [row["volts"] for row in rows] == ["5.00", "5.00000001", "5.00"]
    assert record["sample_count"] == 3
    assert record["termination"] == "operator_stop"
    assert record["error"] is None
    assert record["events_csv"] == "run.events.csv"
    assert record["acquisition_errors"] == {
        "total": 1, "read": 1, "device_clear": 0, "consecutive": 0, "max_consecutive": 1,
    }
    event, = read_events(path)
    assert event["phase"] == "read"
    assert event["error_type"] == (type(bad).__name__ if isinstance(bad, Exception) else "MeterError")
    assert event["consecutive_failures"] == "1"
    assert meter.clears == 1
    assert meter.closed


def test_good_reads_reset_the_limit_and_sustained_failure_stops(monkeypatch, tmp_path):
    meter = Meter(["bad", "5.00", "bad", "5.01", "bad", "bad", "5.02"])
    install_fake(monkeypatch, meter)
    path = tmp_path / "run.csv"
    assert logger.main([
        "--resource", "GPIB0::22::INSTR", "--interval", ".001",
        "--max-consecutive-failures", "2", "--out", str(path),
    ]) == 1
    rows, record = output(path)
    assert [row["volts"] for row in rows] == ["5.00", "5.01"]
    assert record["termination"] == "failed"
    assert "2 consecutive acquisition failures" in record["error"]
    assert record["acquisition_errors"] == {
        "total": 4, "read": 4, "device_clear": 0, "consecutive": 2, "max_consecutive": 2,
    }
    assert [event["consecutive_failures"] for event in read_events(path)] == ["1", "1", "1", "2"]
    assert meter.closed


def test_failed_device_clear_counts_without_sending_a_new_read(monkeypatch, tmp_path):
    class MeterWithStaleResponse(Meter):
        def clear(self):
            super().clear()
            raise TimeoutError("could not clear pending response")

    meter = MeterWithStaleResponse([TimeoutError("read timed out"), "5.00"])
    install_fake(monkeypatch, meter)
    path = tmp_path / "run.csv"
    assert logger.main([
        "--resource", "GPIB0::22::INSTR", "--interval", ".001",
        "--max-consecutive-failures", "2", "--out", str(path),
    ]) == 1
    rows, record = output(path)
    assert rows == []
    assert record["acquisition_errors"]["device_clear"] == 1
    assert [event["phase"] for event in read_events(path)] == ["read", "device_clear"]
    assert next(meter.samples) == "5.00"
    assert meter.closed


def test_sigint_and_zero_baseline(monkeypatch, tmp_path):
    meter = Meter(["0", "stop"])
    install_fake(monkeypatch, meter)
    path = tmp_path / "run.csv"
    assert logger.main(["--resource", "GPIB0::22::INSTR", "--out", str(path)]) == 0
    rows, record = output(path)
    assert [row["volts"] for row in rows] == ["0", "5.00"]
    assert [row["delta_ppm"] for row in rows] == ["", ""]
    assert record["termination"] == "operator_stop"
    assert meter.closed


@pytest.mark.parametrize("flag,value", [
    ("--interval", "-1"), ("--interval", "nan"), ("--duration", "0s"),
    ("--duration", "nan"), ("--resource", "GPIB0::31::INSTR"),
    ("--timeout-ms", "0"), ("--max-consecutive-failures", "0"),
])
def test_invalid_args_fail_before_output(tmp_path, flag, value):
    with pytest.raises(SystemExit) as error:
        logger.main(["--resource", "GPIB0::22::INSTR", flag, value, "--out", str(tmp_path / "run.csv")])
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("result", [(None, 0), (0x120, 0), (4, 0), (0, -1)])
def test_pyvisa_py_go_to_local_uses_status_and_addresses_only_the_selected_meter(result):
    calls = []

    class Bus:
        def send_command(self, commands):
            calls.append((self.timeout, commands))
            return result

    class Manager:
        @contextmanager
        def open_resource(self, resource):
            assert resource == "GPIB0::INTFC"
            yield Bus()

    if result[1] < 0:
        with pytest.raises(MeterError, match="Go To Local failed"):
            go_local(Manager(), None, "GPIB0::22::INSTR", "@py", 30000, None)
    else:
        go_local(Manager(), None, "GPIB0::22::INSTR", "@py", 30000, None)
    assert calls == [(30000, b"\x3f\x36\x01\x3f")]


def test_slow_reads_skip_missed_starts_without_running_past_deadline(monkeypatch, tmp_path):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    starts = []

    class SlowMeter(Meter):
        def query(self, command):
            if command == "READ?":
                starts.append(clock.now)
                clock.sleep(2.5)
            return super().query(command)

    monkeypatch.setattr(logger, "time", clock)
    install_fake(monkeypatch, SlowMeter(["5.00", "5.01"]))
    path = tmp_path / "run.csv"
    assert logger.main(["--resource", "GPIB0::22::INSTR", "--duration", "5s", "--interval", "1", "--out", str(path)]) == 0
    rows, record = output(path)
    assert starts == [0.0, 3.0]
    assert [float(row["elapsed_s"]) for row in rows] == [2.5, 5.5]
    assert record["missed_starts"] == 3
    assert record["termination"] == "duration_elapsed"


@pytest.mark.parametrize("source", [[], ["--simulate", "--resource", "GPIB0::22::INSTR"]])
def test_requires_exactly_one_acquisition_source(tmp_path, source):
    path = tmp_path / "run.csv"
    with pytest.raises(SystemExit) as error:
        logger.main([*source, "--out", str(path)])
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())
