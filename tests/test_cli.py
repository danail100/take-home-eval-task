"""CLI: help text for every subcommand and clean usage errors for bad paths."""

from pathlib import Path

import pytest

from evalkit.cli import main

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _in_repo_root(monkeypatch):
    # The CLI's defaults (tasks/, fixtures/, configs/...) are relative to the current directory.
    monkeypatch.chdir(ROOT)


@pytest.mark.parametrize("cmd", ["infer", "grade", "compare", "demo"])
def test_help_works_for_each_subcommand(cmd, capsys):
    with pytest.raises(SystemExit) as e:
        main([cmd, "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert f"usage: evalkit {cmd}" in out
    assert "(default: tasks)" in out


def test_demo_help_shows_config_metavars_and_defaults(capsys):
    with pytest.raises(SystemExit):
        main(["demo", "--help"])
    out = capsys.readouterr().out
    assert "--config-a, --a CONFIG_A" in out
    assert "--config-b, --b CONFIG_B" in out
    assert "configs/baseline.yaml" in out and "configs/candidate.yaml" in out


@pytest.mark.parametrize("flag", ["--a", "--config-a"])
def test_demo_bad_config_is_a_clean_usage_error_with_suggestion(flag, capsys):
    with pytest.raises(SystemExit) as e:
        main(["demo", flag, "baseline"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "usage: evalkit demo" in err
    assert "'baseline'" in err
    assert "did you mean 'configs/baseline.yaml'" in err
    assert "Traceback" not in err


def test_demo_missing_config_without_suggestion(capsys):
    with pytest.raises(SystemExit) as e:
        main(["demo", "--b", "no/such/config.yaml"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "no/such/config.yaml" in err and "did you mean" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["infer", "nope.yaml"],
        ["infer", "configs/baseline.yaml", "--fixtures", "no_such_dir"],
        ["infer", "configs/baseline.yaml", "--tasks", "no_such_dir"],
        ["grade", "runs/no_such_run"],
        ["compare", "runs/no_such_a", "runs/no_such_b"],
        ["demo", "--tasks", "no_such_dir"],
    ],
)
def test_bad_paths_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2
    assert "error:" in capsys.readouterr().err
