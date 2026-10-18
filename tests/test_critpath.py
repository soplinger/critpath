import json
from datetime import date

import pytest

from critpath import (
    Calendar,
    ScheduleError,
    find_cycle,
    main,
    parse,
    parse_calendar,
    parse_date,
    render_gantt,
    schedule,
    to_dict,
    what_if_dict,
)

BASIC = """\
start 2026-11-02
calendar mon-fri
task spec 2d
task design 3d after spec
task build 5d after design
task docs 2d after spec
milestone launch after build, docs
"""


def _schedule(text, delays=None):
    project, issues = parse(text)
    assert not [i for i in issues if i.severity == "error"], issues
    return schedule(project, delays)


def _row(sched, task_id):
    return next(r for r in sched.rows if r.id == task_id)


def test_parse_reads_tasks_dependencies_and_start():
    project, issues = parse(BASIC)
    assert issues == []
    assert project.start == date(2026, 11, 2)
    assert list(project.tasks) == ["spec", "design", "build", "docs", "launch"]
    assert project.tasks["design"].duration == 3
    assert project.tasks["design"].deps == ["spec"]
    assert project.tasks["launch"].kind == "milestone"
    assert project.tasks["launch"].deps == ["build", "docs"]


def test_comments_blank_lines_and_spacing_are_tolerated():
    text = """
    # a comment line
    start   2026-11-02   # trailing comment

    task   a 1d
    task b 2   after   a ,  a
    """
    project, issues = parse(text)
    assert project.tasks["b"].duration == 2
    assert project.tasks["b"].deps == ["a", "a"]
    assert [i.severity for i in issues] == ["warning"]
    assert "duplicate dependency 'a'" in issues[0].message


def test_parse_errors_carry_line_numbers():
    text = "start 2026-11-02\ntask a 1d\ntaks b 2d\ntask c 3x\ntask d 1d before a\n"
    _, issues = parse(text)
    messages = [str(i) for i in issues]
    assert "line 3: error: unknown keyword 'taks'" in messages
    assert "line 4: error: task 'c' needs a duration in working days, e.g. 'c 3d'" in messages
    assert "line 5: error: unexpected 'before' after 'd'; dependencies start with 'after'" in messages


def test_duplicate_task_id_points_at_first_definition():
    text = "start 2026-11-02\ntask a 1d\ntask a 2d\n"
    _, issues = parse(text)
    assert len(issues) == 1
    assert issues[0].line == 3
    assert "first defined on line 2" in issues[0].message


def test_unknown_and_self_dependencies_are_errors():
    text = "start 2026-11-02\ntask a 1d after ghost\ntask b 1d after b\n"
    _, issues = parse(text)
    messages = {(i.line, i.message) for i in issues if i.severity == "error"}
    assert (2, "unknown dependency 'ghost'") in messages
    assert (3, "task 'b' depends on itself") in messages


def test_cycle_is_reported_as_a_loop():
    text = "start 2026-11-02\ntask a 1d after b\ntask b 1d after c\ntask c 1d after a\n"
    _, issues = parse(text)
    cycle_issues = [i for i in issues if "cycle" in i.message]
    assert len(cycle_issues) == 1
    assert cycle_issues[0].message == "dependency cycle: a -> b -> c -> a"
    with pytest.raises(ScheduleError):
        schedule(parse(text)[0])


def test_find_cycle_returns_none_for_dag():
    project, _ = parse(BASIC)
    assert find_cycle(project.tasks) is None


def test_missing_start_is_an_error_unless_overridden():
    text = "task a 1d\n"
    _, issues = parse(text)
    assert any("no start date" in i.message for i in issues)
    project, issues = parse(text, start=date(2026, 1, 5))
    assert project.start == date(2026, 1, 5)
    assert not [i for i in issues if i.severity == "error"]


def test_zero_duration_task_warns_and_empty_file_errors():
    _, issues = parse("start 2026-11-02\ntask a 0d\n")
    assert [str(i) for i in issues] == [
        "line 2: warning: task 'a' has zero duration; use 'milestone' instead"
    ]
    _, issues = parse("start 2026-11-02\n")
    assert any(i.message == "no tasks defined" for i in issues)


def test_parse_calendar_and_dates():
    assert parse_calendar("mon-fri") == frozenset(range(5))
    assert parse_calendar("every-day") == frozenset(range(7))
    assert parse_calendar("fri-mon") == frozenset({4, 5, 6, 0})
    assert parse_calendar("funday") is None
    assert parse_date("2026-02-30") is None
    assert parse_date("20261102") is None
    assert parse_date("2026-11-02") == date(2026, 11, 2)


def test_calendar_skips_weekends_and_holidays():
    cal = Calendar(date(2026, 11, 2), frozenset(range(5)), {date(2026, 11, 4): "holiday"})
    assert cal.day(0) == date(2026, 11, 2)
    assert cal.day(1) == date(2026, 11, 3)
    assert cal.day(2) == date(2026, 11, 5)  # Wednesday is a holiday
    assert cal.day(4) == date(2026, 11, 9)  # Monday after the weekend
    assert cal.count_through(date(2026, 11, 5)) == 3


def test_calendar_rolls_a_weekend_start_forward():
    cal = Calendar(date(2026, 11, 7), frozenset(range(5)), {})
    assert cal.day(0) == date(2026, 11, 9)


def test_forward_and_backward_pass_find_slack_and_critical_path():
    sched = _schedule(BASIC)
    assert sched.finish == date(2026, 11, 13)
    assert sched.finish_index == 10
    assert sched.critical_path == ["spec", "design", "build", "launch"]
    docs = _row(sched, "docs")
    assert docs.slack == 6
    assert not docs.critical
    assert _row(sched, "build").slack == 0


def test_task_dates_follow_the_working_day_calendar():
    sched = _schedule(BASIC)
    design = _row(sched, "design")
    assert design.start == date(2026, 11, 4)
    assert design.finish == date(2026, 11, 6)
    build = _row(sched, "build")
    assert build.start == date(2026, 11, 9)  # weekend skipped
    assert build.finish == date(2026, 11, 13)


def test_milestone_lands_on_last_day_of_its_predecessors():
    sched = _schedule(BASIC)
    launch = _row(sched, "launch")
    assert launch.start == launch.finish == date(2026, 11, 13)


def test_due_date_reports_working_days_late():
    text = BASIC + "due launch 2026-11-10\ndue docs 2026-11-30\n"
    sched = _schedule(text)
    assert _row(sched, "launch").late_by == 3
    assert _row(sched, "docs").late_by == 0
    assert [r.id for r in sched.late] == ["launch"]


def test_due_date_for_unknown_task_is_an_error():
    _, issues = parse(BASIC + "due nothing 2026-11-30\n")
    assert any("unknown task 'nothing'" in i.message for i in issues)


def test_what_if_delay_moves_the_finish():
    base = _schedule(BASIC)
    alt = _schedule(BASIC, {"spec": 2})
    assert alt.finish_index - base.finish_index == 2
    assert alt.finish == date(2026, 11, 17)
    data = what_if_dict(base, alt, {"spec": 2})
    assert data["shift_working_days"] == 2
    assert "spec" in data["moved"]


def test_delaying_a_milestone_or_unknown_task_is_refused():
    project, _ = parse(BASIC)
    with pytest.raises(ScheduleError):
        schedule(project, {"launch": 1})
    with pytest.raises(ScheduleError):
        schedule(project, {"ghost": 1})


def test_gantt_marks_critical_work_slack_and_milestone():
    gantt = render_gantt(_schedule(BASIC))
    assert "spec   |##        |" in gantt
    assert "design |  ###     |" in gantt
    assert "build  |     #####|" in gantt
    assert "docs   |  ==......|" in gantt
    assert "launch |         M|" in gantt


def test_json_output_has_the_expected_shape():
    data = to_dict(_schedule(BASIC))
    assert data["finish"] == "2026-11-13"
    assert data["critical_path"] == ["spec", "design", "build", "launch"]
    assert [t["id"] for t in data["tasks"]][0] == "spec"
    json.dumps(data)  # must be serialisable


def test_cli_check_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.txt"
    good.write_text(BASIC, encoding="utf-8")
    assert main(["check", str(good)]) == 0
    assert "OK: 5 tasks" in capsys.readouterr().out

    bad = tmp_path / "bad.txt"
    bad.write_text("start 2026-11-02\ntask a\n", encoding="utf-8")
    assert main(["check", str(bad)]) == 1
    assert "line 2: error" in capsys.readouterr().err


def test_cli_fail_late_exit_code(tmp_path, capsys):
    path = tmp_path / "late.txt"
    path.write_text(BASIC + "due launch 2026-11-10\n", encoding="utf-8")
    assert main(["schedule", str(path)]) == 0
    capsys.readouterr()
    assert main(["schedule", str(path), "--fail-late"]) == 3
    capsys.readouterr()
    assert main(["schedule", str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["late"] == ["launch"]
