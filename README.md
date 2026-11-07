# DC Reference Logger

A standalone Python starter for logging a DC voltage reference with an HP 34401A over GPIB. The same acquisition code measures any DC reference suitable for the meter's fixed 10 V range. Use Python 3.10 or newer.

## Run the simulator

From the repository root:

```sh
python3 -m dc_reference_logger --simulate --duration 5s --reference "Example 5 V standard"
```

The simulator needs only the Python standard library. The command writes a voltage CSV, an events CSV, and a JSON run record under `runs/`. All three share a filename stem. Use `--out runs/example.csv` to choose a new stem. The reference label is optional and lives in the JSON.

## Run on the meter

Set up the operating system's GPIB driver and connect the reference to the meter's selected DC voltage terminals. Activate a virtual environment and install the hardware extra:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[hardware]'
pyvisa-info
```

Replace the resource in this example with your meter's actual GPIB address:

```sh
python -m dc_reference_logger --resource GPIB0::22::INSTR --duration 15s \
  --reference "Example 5 V standard" --note "Initial bench check"
```

Confirm the JSON records the expected instrument ID, `configuration_verified: true`, and `termination: "duration_elapsed"`. Check that the meter returns to local control at the end. Omit `--duration` for a continuous run and press Ctrl-C to stop. Hardware defaults to back-to-back readings. Use `--interval` to request an interval between reading starts.

The HP 34401A profile defaults to fixed 10 V DC, 100 NPLC, autozero on, and automatic input impedance. The CLI also accepts `--nplc`. The results describe the instrument readings and run conditions. Calibration and uncertainty require their own measurement procedure.

## Project layout

| Location | Responsibility |
| --- | --- |
| `dc_reference_logger/meter.py` | HP 34401A SCPI setup and readback, PyVISA session management, and simulator. |
| `dc_reference_logger/logger.py` | CLI, acquisition timing, safe output creation, and the run record. |
| `tests/` | Hardware-free checks for configuration, output protection, timing, and interrupted runs. |

The CSV contains `timestamp,elapsed_s,volts,delta_ppm`. Ppm is relative to the first reading and is blank when that reading is zero. Each timestamp includes its UTC offset and records when the host received the meter response. Elapsed time uses a monotonic clock. The JSON holds the run ID, reference label, meter settings, counts, statistics, and outcome. A forcibly killed process can leave the run marked `running`. In that case, use the complete CSV rows to see what was captured.

## Acquisition errors and retries

A timeout, transport error, garbled reply, or out-of-range reading is recorded in `<stem>.events.csv`. The file includes the host timestamp, elapsed time, error phase and type, message, and consecutive failure count. Failed attempts leave gaps in the voltage CSV. The logger flushes each event as it occurs and records totals, counts by phase, and the longest failure streak in the final JSON.

The next attempt starts with an addressed device clear to discard a possible pending reply. If that clear fails, the attempt counts as a failure and the logger waits until the next attempt to clear again. A valid reading resets the consecutive count. Retries use the next scheduled interval, or wait one second in back-to-back mode. Acquisition stops with a nonzero exit status at **five consecutive failures** by default. Use `--max-consecutive-failures N` to set a different threshold. Setup errors and failures writing the run files still terminate the run.

## Next issue — optional environmental input

Add an Arduino that reports temperature and relative humidity as simple records over USB serial. The host should timestamp those records independently of the voltage loop and save them in an optional `environment.csv` linked by the run ID. A sensor disconnect or malformed record should leave a gap in environmental data while the meter continues sampling. Choose the board and wiring after identifying the sensor breakout and its signal levels.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

GitHub Actions runs the hardware-free suite. Keep bench runs and personal setup notes in local run records. Use one issue, branch, and pull request for each independently verifiable change.

[Keysight 34401A user guide](https://www.keysight.com/us/en/assets/9018-01063/user-manuals/9018-01063.pdf) · [PyVISA-py installation and GPIB support](https://github.com/pyvisa/pyvisa-py/blob/main/docs/source/installation.rst)

## License

[MIT](LICENSE).
