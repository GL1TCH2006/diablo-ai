from tools.python import run_script


def test_run_hello_world(tmp_settings):
    script = tmp_settings.scratch_dir / "hello.py"
    script.write_text('print("Hello World")\n', encoding="utf-8")
    result = run_script(str(script), timeout=60)
    assert result.success
    assert result.output.strip() == "Hello World"
    assert result.exit_code == 0


def test_run_missing_script(tmp_settings):
    result = run_script(str(tmp_settings.scratch_dir / "missing.py"), timeout=60)
    assert not result.success
    assert "not found" in result.error.lower()


def test_run_failing_script(tmp_settings):
    script = tmp_settings.scratch_dir / "fail.py"
    script.write_text("print('before')\nraise SystemExit(3)\n", encoding="utf-8")
    result = run_script(str(script), timeout=60)
    assert not result.success
    assert result.exit_code == 3