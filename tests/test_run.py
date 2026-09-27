from morning_brief import daily_worker, pipeline, run


def test_daily_brief_off_refuses_generate_without_calling_the_api(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FEED_TOKEN", "x" * 32)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAILY_BRIEF", "0")

    def boom(settings):
        raise AssertionError("pipeline.default_deps should not be called when the daily brief is off")

    monkeypatch.setattr(pipeline, "default_deps", boom)

    code = run.main([])

    assert code == 2
    assert "daily brief is off (DAILY_BRIEF=0); not calling the API" in capsys.readouterr().out


def test_worker_mode_gathers_without_calling_the_api(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FEED_TOKEN", "x" * 32)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAILY_BRIEF", "worker")
    seen = []

    def boom(settings):
        raise AssertionError("pipeline.default_deps should not be called in worker mode")

    monkeypatch.setattr(pipeline, "default_deps", boom)
    monkeypatch.setattr(daily_worker, "gather_run", lambda s, trigger: seen.append(trigger) or "2026-09-25")

    assert run.main([]) == 0
    assert seen == ["cli"]
    assert "Gathered today's stories; the Mac worker writes the brief." in capsys.readouterr().out

    monkeypatch.setattr(daily_worker, "gather_run", lambda s, trigger: None)
    assert run.main([]) == 1
    assert "Nothing gathered; see the logs." in capsys.readouterr().out
