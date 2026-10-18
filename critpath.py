#!/usr/bin/env python3
"""critpath: offline critical-path scheduler for plain-text task lists.

Reads a small plain-text project file, works out early and latest dates on a
working-day calendar, and reports slack, the critical path, missed due dates,
what-if delays and a Gantt chart. Standard library only.
"""

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

ONE_DAY = timedelta(days=1)
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_WORKDAYS = frozenset(range(5))
ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
DURATION_RE = re.compile(r"(\d+)d?\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
CALENDAR_RE = re.compile(r"([a-z]{3})-([a-z]{3})\Z")
DELAY_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)=(\d+)\Z")


class ScheduleError(Exception):
    """Raised when a project cannot be scheduled."""


@dataclass
class Issue:
    severity: str  # "error" or "warning"
    message: str
    line: int | None = None

    def __str__(self):
        where = f"line {self.line}: " if self.line else ""
        return f"{where}{self.severity}: {self.message}"


@dataclass
class Task:
    id: str
    kind: str  # "task" or "milestone"
    duration: int  # working days; always 0 for milestones
    deps: list
    line: int


@dataclass
class Project:
    start: date | None = None
    workdays: frozenset = DEFAULT_WORKDAYS
    holidays: dict = field(default_factory=dict)  # date -> name
    tasks: dict = field(default_factory=dict)  # id -> Task, in file order
    dues: dict = field(default_factory=dict)  # id -> (date, line)


@dataclass
class Row:
    id: str
    kind: str
    duration: int
    deps: list
    early_start: int  # working-day indexes, counted from the project start
    early_finish: int
    latest_start: int
    latest_finish: int
    start: date
    finish: date
    latest_start_date: date
    latest_finish_date: date
    slack: int
    critical: bool
    due: date | None
    late_by: int


@dataclass
class Schedule:
    project: Project
    calendar: "Calendar"
    rows: list
    finish_index: int
    finish: date
    critical_path: list

    @property
    def late(self):
        return [r for r in self.rows if r.late_by > 0]


# ---------------------------------------------------------------- parsing


def parse_date(text):
    """Return a date for a strict YYYY-MM-DD string, otherwise None."""
    if not DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_calendar(text):
    """Return the set of weekday numbers for 'mon-fri', 'mon-sat' or 'every-day'."""
    value = text.lower()
    if value == "every-day":
        return frozenset(range(7))
    match = CALENDAR_RE.match(value)
    if not match or match.group(1) not in DAY_NAMES or match.group(2) not in DAY_NAMES:
        return None
    day = DAY_NAMES.index(match.group(1))
    last = DAY_NAMES.index(match.group(2))
    days = {day}
    while day != last:
        day = (day + 1) % 7
        days.add(day)
    return frozenset(days)


def parse(text, start=None):
    """Parse the project format. Returns (Project, issues).

    A start date passed in overrides any 'start' line in the text.
    """
    project = Project()
    issues = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        words = raw.split("#", 1)[0].split()
        if words:
            _parse_line(project, words, lineno, issues)
    if start is not None:
        project.start = start
    validate(project, issues)
    return project, issues


def _parse_line(project, words, lineno, issues):
    def error(message):
        issues.append(Issue("error", message, lineno))

    def warning(message):
        issues.append(Issue("warning", message, lineno))

    keyword, args = words[0].lower(), words[1:]

    if keyword == "start":
        when = parse_date(args[0]) if len(args) == 1 else None
        if when is None:
            error("expected 'start YYYY-MM-DD'")
        elif project.start is not None:
            error("start date is already set")
        else:
            project.start = when

    elif keyword == "calendar":
        workdays = parse_calendar(args[0]) if len(args) == 1 else None
        if workdays is None:
            error("expected 'calendar mon-fri', 'calendar mon-sat' or 'calendar every-day'")
        else:
            project.workdays = workdays

    elif keyword == "holiday":
        when = parse_date(args[0]) if args else None
        if when is None:
            error("expected 'holiday YYYY-MM-DD [name]'")
        elif when in project.holidays:
            warning(f"holiday {when.isoformat()} is listed twice")
        else:
            project.holidays[when] = " ".join(args[1:])

    elif keyword in ("task", "milestone"):
        _parse_task(project, keyword, args, lineno, error)

    elif keyword == "due":
        when = parse_date(args[1]) if len(args) == 2 else None
        if when is None:
            error("expected 'due TASK-ID YYYY-MM-DD'")
        elif args[0] in project.dues:
            error(f"'{args[0]}' already has a due date")
        else:
            project.dues[args[0]] = (when, lineno)

    else:
        error(f"unknown keyword '{words[0]}'")


def _parse_task(project, kind, args, lineno, error):
    task_id = args[0] if args else ""
    if not ID_RE.match(task_id):
        error(f"{kind} needs an ID starting with a letter, got '{task_id}'")
        return
    rest = args[1:]
    duration = 0
    if kind == "task":
        match = DURATION_RE.match(rest[0]) if rest else None
        if match is None:
            error(f"task '{task_id}' needs a duration in working days, e.g. '{task_id} 3d'")
            return
        duration = int(match.group(1))
        rest = rest[1:]
    deps = []
    if rest:
        if rest[0].lower() != "after":
            error(f"unexpected '{rest[0]}' after '{task_id}'; dependencies start with 'after'")
            return
        parts = [part.strip() for part in " ".join(rest[1:]).split(",")]
        if not all(ID_RE.match(part) for part in parts):
            error(f"'after' needs a comma-separated list of task IDs for '{task_id}'")
            return
        deps = parts
    if task_id in project.tasks:
        first = project.tasks[task_id].line
        error(f"duplicate task ID '{task_id}' (first defined on line {first})")
        return
    project.tasks[task_id] = Task(task_id, kind, duration, deps, lineno)


def validate(project, issues):
    """Add semantic errors and warnings that need the whole file."""
    if project.start is None:
        issues.append(Issue("error", "no start date; add 'start YYYY-MM-DD' or pass --start"))
    if not project.tasks:
        issues.append(Issue("error", "no tasks defined"))
    for task in project.tasks.values():
        if task.kind == "task" and task.duration == 0:
            issues.append(Issue(
                "warning",
                f"task '{task.id}' has zero duration; use 'milestone' instead",
                task.line,
            ))
        seen = set()
        for dep in task.deps:
            if dep == task.id:
                issues.append(Issue("error", f"task '{task.id}' depends on itself", task.line))
            elif dep not in project.tasks:
                issues.append(Issue("error", f"unknown dependency '{dep}'", task.line))
            elif dep in seen:
                issues.append(Issue("warning", f"duplicate dependency '{dep}'", task.line))
            seen.add(dep)
    for task_id, (_, line) in project.dues.items():
        if task_id not in project.tasks:
            issues.append(Issue("error", f"due date for unknown task '{task_id}'", line))
    cycle = find_cycle(project.tasks)
    if cycle:
        first = project.tasks[cycle[0]].line
        issues.append(Issue("error", "dependency cycle: " + " -> ".join(cycle), first))


def find_cycle(tasks):
    """Return one dependency loop as a list of IDs (first equals last), or None."""
    state = {}
    path = []

    def visit(task_id):
        state[task_id] = "active"
        path.append(task_id)
        for dep in tasks[task_id].deps:
            if dep not in tasks or dep == task_id:
                continue
            if state.get(dep) == "active":
                return path[path.index(dep):] + [dep]
            if dep not in state:
                loop = visit(dep)
                if loop:
                    return loop
        path.pop()
        state[task_id] = "done"
        return None

    for task_id in tasks:
        if task_id not in state:
            loop = visit(task_id)
            if loop:
                return loop
    return None


# ---------------------------------------------------------------- calendar


class Calendar:
    """Maps working-day indexes (0, 1, 2, ...) onto real calendar dates."""

    def __init__(self, start, workdays, holidays):
        self.start = start
        self.workdays = workdays
        self.holidays = holidays
        self._days = []

    def is_working(self, day):
        return day.weekday() in self.workdays and day not in self.holidays

    def day(self, index):
        """Date of the working day at this index. A non-working start rolls forward."""
        while len(self._days) <= index:
            cursor = self._days[-1] + ONE_DAY if self._days else self.start
            while not self.is_working(cursor):
                cursor += ONE_DAY
            self._days.append(cursor)
        return self._days[index]

    def count_through(self, when):
        """Number of working days on or before the given date."""
        count = 0
        while self.day(count) <= when:
            count += 1
        return count


def _start_date(cal, index, duration):
    return cal.day(index) if duration else _finish_date(cal, index)


def _finish_date(cal, index):
    # Index is the exclusive end of the work, so the last day worked is index - 1.
    return cal.day(max(index - 1, 0))


# ---------------------------------------------------------------- scheduling


def topo_order(tasks):
    """Task IDs with every dependency listed before its dependents."""
    order = []
    seen = set()

    def visit(task_id):
        if task_id in seen:
            return
        seen.add(task_id)
        for dep in tasks[task_id].deps:
            if dep in tasks:
                visit(dep)
        order.append(task_id)

    for task_id in tasks:
        visit(task_id)
    return order


def schedule(project, delays=None):
    """Forward and backward pass over a validated project.

    delays maps task IDs to extra working days, for what-if runs.
    """
    delays = dict(delays or {})
    if project.start is None:
        raise ScheduleError("no start date")
    for task_id in delays:
        if task_id not in project.tasks:
            raise ScheduleError(f"cannot delay unknown task '{task_id}'")
        if project.tasks[task_id].kind == "milestone":
            raise ScheduleError(f"cannot delay milestone '{task_id}'")
    if find_cycle(project.tasks):
        raise ScheduleError("dependency cycle")

    cal = Calendar(project.start, project.workdays, project.holidays)
    duration = {tid: t.duration + delays.get(tid, 0) for tid, t in project.tasks.items()}
    order = topo_order(project.tasks)

    early_start, early_finish = {}, {}
    for tid in order:
        deps = project.tasks[tid].deps
        early_start[tid] = max((early_finish[d] for d in deps), default=0)
        early_finish[tid] = early_start[tid] + duration[tid]
    finish_index = max(early_finish.values())

    successors = {tid: [] for tid in project.tasks}
    for task in project.tasks.values():
        for dep in task.deps:
            successors[dep].append(task.id)
    latest_start, latest_finish = {}, {}
    for tid in reversed(order):
        latest_finish[tid] = min(
            (latest_start[s] for s in successors[tid]), default=finish_index
        )
        latest_start[tid] = latest_finish[tid] - duration[tid]

    rows = []
    for tid, task in project.tasks.items():
        dur = duration[tid]
        slack = latest_start[tid] - early_start[tid]
        due, late_by = None, 0
        if tid in project.dues:
            due = project.dues[tid][0]
            late_by = max(0, early_finish[tid] - cal.count_through(due))
        rows.append(Row(
            id=tid,
            kind=task.kind,
            duration=dur,
            deps=list(task.deps),
            early_start=early_start[tid],
            early_finish=early_finish[tid],
            latest_start=latest_start[tid],
            latest_finish=latest_finish[tid],
            start=_start_date(cal, early_start[tid], dur),
            finish=_finish_date(cal, early_finish[tid]),
            latest_start_date=_start_date(cal, latest_start[tid], dur),
            latest_finish_date=_finish_date(cal, latest_finish[tid]),
            slack=slack,
            critical=slack == 0,
            due=due,
            late_by=late_by,
        ))

    critical = [tid for tid in order if latest_start[tid] - early_start[tid] == 0]
    critical.sort(key=lambda tid: early_start[tid])
    return Schedule(
        project=project,
        calendar=cal,
        rows=rows,
        finish_index=finish_index,
        finish=_finish_date(cal, finish_index),
        critical_path=critical,
    )


# ---------------------------------------------------------------- rendering


def _iso(when):
    return None if when is None else when.isoformat()


def _row_dict(row):
    return {
        "id": row.id,
        "kind": row.kind,
        "duration": row.duration,
        "depends_on": row.deps,
        "early_start": row.early_start,
        "early_finish": row.early_finish,
        "latest_start": row.latest_start,
        "latest_finish": row.latest_finish,
        "start": _iso(row.start),
        "finish": _iso(row.finish),
        "latest_start_date": _iso(row.latest_start_date),
        "latest_finish_date": _iso(row.latest_finish_date),
        "slack": row.slack,
        "critical": row.critical,
        "due": _iso(row.due),
        "late_by": row.late_by,
    }


def to_dict(sched):
    return {
        "start": _iso(sched.calendar.day(0)),
        "finish": _iso(sched.finish),
        "working_days": sched.finish_index,
        "critical_path": sched.critical_path,
        "late": [r.id for r in sched.late],
        "tasks": [_row_dict(r) for r in sched.rows],
    }


def to_csv(sched):
    buf = io.StringIO()
    dicts = [_row_dict(r) for r in sched.rows]
    writer = csv.DictWriter(buf, fieldnames=list(dicts[0]), lineterminator="\n")
    writer.writeheader()
    for item in dicts:
        writer.writerow({
            key: ";".join(value) if isinstance(value, list) else ("" if value is None else value)
            for key, value in item.items()
        })
    return buf.getvalue()


def _table(rows):
    headers = ["ID", "KIND", "DUR", "START", "FINISH", "SLACK", "CRIT", "DUE", "LATE"]
    body = []
    for r in rows:
        body.append([
            r.id,
            r.kind,
            f"{r.duration}d",
            _iso(r.start),
            _iso(r.finish),
            f"{r.slack}d",
            "yes" if r.critical else "",
            _iso(r.due) or "-",
            f"{r.late_by}d" if r.due else "-",
        ])
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *body)]
    lines = [headers] + body
    return "\n".join(
        "  ".join(str(cell).ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in lines
    )


def render_report(sched):
    lines = [
        f"Start {_iso(sched.calendar.day(0))}   Finish {_iso(sched.finish)}"
        f"   ({sched.finish_index} working days)",
        "Critical path: " + " -> ".join(sched.critical_path),
        "",
        _table(sched.rows),
    ]
    if sched.late:
        lines += ["", "Missed due dates:"]
        lines += [
            f"  {r.id}: due {_iso(r.due)}, late by {r.late_by} working day(s)"
            for r in sched.late
        ]
    return "\n".join(lines)


def render_gantt(sched):
    width = sched.finish_index
    if width == 0:
        return "(nothing to chart)"
    name_w = max(len(r.id) for r in sched.rows)
    ruler = "".join(str(sched.calendar.day(i).day % 10) for i in range(width))
    lines = [
        f"Gantt {_iso(sched.calendar.day(0))} .. {_iso(sched.finish)}   "
        "# critical  = normal  . slack  M milestone",
        f"{'':<{name_w}} |{ruler}|",
    ]
    for r in sched.rows:
        cells = []
        for col in range(width):
            if r.kind == "milestone":
                cell = "M" if col == max(r.early_finish - 1, 0) else " "
            elif r.early_start <= col < r.early_finish:
                cell = "#" if r.critical else "="
            elif r.early_finish <= col < r.latest_finish:
                cell = "."
            else:
                cell = " "
            cells.append(cell)
        lines.append(f"{r.id.ljust(name_w)} |{''.join(cells)}|")
    return "\n".join(lines)


def _moved(base, alt):
    before = {r.id: r.finish for r in base.rows}
    return [r.id for r in alt.rows if r.finish != before[r.id]]


def what_if_dict(base, alt, delays):
    return {
        "delays": delays,
        "finish": _iso(alt.finish),
        "shift_working_days": alt.finish_index - base.finish_index,
        "moved": _moved(base, alt),
    }


def render_what_if(base, alt, delays):
    shift = alt.finish_index - base.finish_index
    change = "no change" if shift == 0 else f"{shift:+d} working day(s)"
    applied = ", ".join(f"{tid}+{days}d" for tid, days in delays.items())
    lines = [
        f"What-if ({applied}): finish {_iso(base.finish)} -> {_iso(alt.finish)} ({change})"
    ]
    moved = _moved(base, alt)
    if moved:
        lines.append("  finish moved for: " + ", ".join(moved))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


def _date_arg(text):
    when = parse_date(text)
    if when is None:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text}")
    return when


def _delay_arg(text):
    match = DELAY_RE.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"expected ID=DAYS, got '{text}'")
    return match.group(1), int(match.group(2))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="critpath",
        description="Offline critical-path scheduler for plain-text task lists.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="lint a project file")
    check.add_argument("file", help="project file, or - for stdin")
    check.add_argument("--start", type=_date_arg, help="override the start date")
    check.add_argument("--strict", action="store_true", help="treat warnings as errors")

    schedule_cmd = sub.add_parser("schedule", help="print dates, slack and the critical path")
    schedule_cmd.add_argument("file", help="project file, or - for stdin")
    schedule_cmd.add_argument("--start", type=_date_arg, help="override the start date")
    schedule_cmd.add_argument(
        "--format", choices=["text", "json", "csv"], default="text", help="output format"
    )
    schedule_cmd.add_argument(
        "--delay", type=_delay_arg, action="append",
        help="what-if: add DAYS to task ID, e.g. --delay build=3 (repeatable)",
    )
    schedule_cmd.add_argument(
        "--fail-late", action="store_true",
        help="exit 3 if the scenario shown misses a due date",
    )

    gantt = sub.add_parser("gantt", help="draw a text Gantt chart")
    gantt.add_argument("file", help="project file, or - for stdin")
    gantt.add_argument("--start", type=_date_arg, help="override the start date")
    return parser


def _read(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _report(issues):
    for issue in issues:
        print(issue, file=sys.stderr)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        text = _read(args.file)
    except OSError as exc:
        print(f"critpath: cannot read {args.file}: {exc.strerror}", file=sys.stderr)
        return 1

    project, issues = parse(text, start=args.start)
    _report(issues)
    if any(issue.severity == "error" for issue in issues):
        return 1

    if args.command == "check":
        warnings = [issue for issue in issues if issue.severity == "warning"]
        if args.strict and warnings:
            return 1
        deps = sum(len(task.deps) for task in project.tasks.values())
        print(f"OK: {len(project.tasks)} tasks, {deps} dependencies")
        return 0

    try:
        base = schedule(project)
        alt = schedule(project, dict(args.delay)) if args.delay else None
    except ScheduleError as exc:
        print(f"critpath: {exc}", file=sys.stderr)
        return 1

    if args.command == "gantt":
        print(render_gantt(base))
        return 0

    shown = alt or base
    if args.format == "json":
        data = to_dict(base)
        if alt:
            data["what_if"] = what_if_dict(base, alt, dict(args.delay))
        print(json.dumps(data, indent=2))
    elif args.format == "csv":
        print(to_csv(base), end="")
    else:
        print(render_report(base))
        if alt:
            print()
            print(render_what_if(base, alt, dict(args.delay)))

    if args.fail_late and shown.late:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
