# Critpath

**Offline critical-path scheduler for plain-text task lists.**

Describe a project in a small text file: tasks, durations in working days, and
what each one waits for. Critpath works out the earliest and latest dates on a
working-day calendar, finds the slack on every task, prints the critical path,
flags missed due dates, runs what-if delays, and draws a Gantt chart. It is one
Python file, uses only the standard library, and never touches the network.

## Features

- **Plain-text project format**: `task`, `milestone`, `start`, `calendar`,
  `holiday`, and `due` lines. Comments with `#`, blank lines and loose spacing are fine.
- **Line-numbered errors**: every problem is reported with its line, for example
  `line 3: error: unknown keyword 'taks'`.
- **Dependency checks**: unknown IDs, self-dependencies, duplicate IDs, and
  dependency cycles. A cycle is printed as the actual loop, such as `a -> b -> c -> a`.
- **Working-day calendar**: `calendar mon-fri`, `mon-sat`, `every-day` or any
  weekday range, plus a holiday list with names. Weekend starts roll forward to the next working day.
- **Forward and backward pass**: early start and finish, latest start and finish,
  and slack for every task.
- **Critical path**: the chain of zero-slack tasks, in order.
- **Milestones**: zero-length markers that fall on the last working day their predecessors finish.
- **Due dates**: `due TASK DATE` lines report how many working days late a task finishes.
- **What-if delays**: `--delay build=3` shows how the finish date and the affected tasks move.
- **Gantt chart**: a text chart with `#` for critical work, `=` for normal work,
  `.` for slack, and `M` for milestones.
- **Output formats**: an aligned text report, JSON, or CSV for spreadsheets and CI.
- **Lint**: `check` reports errors and warnings, and `--strict` turns warnings into failures.
- **CI-friendly exit codes**: `0` success, `1` errors in the file, `3` a due date is missed (with `--fail-late`).

## Quick start

Save this as `kitchen.txt`, or use the copy in `examples/kitchen-remodel.txt`:

```text
# Kitchen remodel
start 2026-11-02
calendar mon-fri
holiday 2026-11-26 Thanksgiving

task permits 5d
task demo 2d after permits
task cabinets 6d after demo
task plumbing 3d after demo
task tile 4d after plumbing, cabinets
task paint 2d after tile
task inspect 1d after plumbing
milestone handover after paint, inspect

due handover 2026-12-04
```

Then run:

```sh
python3 critpath.py check examples/kitchen-remodel.txt
python3 critpath.py schedule examples/kitchen-remodel.txt
python3 critpath.py schedule examples/kitchen-remodel.txt --delay cabinets=3
python3 critpath.py gantt examples/kitchen-remodel.txt
python3 critpath.py schedule examples/kitchen-remodel.txt --format csv > plan.csv
```

## Requirements

- Python 3.10 or newer. The code uses `X | None` annotations.
- Nothing to install to run it. Tests need `pytest`.

## How to run

```sh
python3 critpath.py <command> <file> [options]
```

| Command    | What it does                                                    |
|------------|-----------------------------------------------------------------|
| `check`    | Lint the file. Add `--strict` to fail on warnings.              |
| `schedule` | Print dates, slack, critical path and missed due dates. Options: `--format text\|json\|csv`, `--delay ID=DAYS` (repeatable), `--fail-late`. |
| `gantt`    | Draw the text Gantt chart.                                      |

Every command accepts `--start YYYY-MM-DD` to override the file's start date.
Pass `-` as the file to read from standard input.

### The file format

| Line                                   | Meaning                                              |
|----------------------------------------|------------------------------------------------------|
| `start 2026-11-02`                     | Project start. Required, or pass `--start`.          |
| `calendar mon-fri`                     | Working weekdays. Also `mon-sat`, `every-day`.       |
| `holiday 2026-11-26 Thanksgiving`      | Non-working date with an optional name.              |
| `task ID 3d`                           | Task lasting 3 working days. The `d` is optional.    |
| `task ID 3d after A, B`                | Task that starts after A and B both finish.          |
| `milestone ID after A, B`              | Zero-length marker.                                  |
| `due ID 2026-12-04`                    | Deadline for a task or milestone.                    |

Task IDs start with a letter and may contain letters, digits, `-` and `_`.

## How to test

```sh
python3 -m pip install pytest
python3 -m pytest
```

The tests cover parsing and its error messages, cycle detection, calendar
rules (weekends, holidays, weekend starts), the forward and backward passes,
slack and the critical path, due dates, what-if delays, Gantt output, JSON
shape, and CLI exit codes.

## Project layout

```text
critpath.py              the whole tool: parser, scheduler, renderers, CLI
examples/kitchen-remodel.txt   sample project
tests/test_critpath.py   pytest suite
pytest.ini               puts the project root on the import path
```

## Notes

- Durations are whole working days. A task of `3d` that starts on day `n` occupies days `n` to `n+2`.
- Milestones and tasks that finish on the same working day share that date.
- With the default calendar, a task that starts on a Friday and lasts one day finishes on that Friday.
