from morning_brief import pipeline, run


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
