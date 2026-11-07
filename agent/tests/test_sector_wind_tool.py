import json

from src.tools.sector_wind_tool import SectorWindTool


def test_bad_action_and_horizon():
    t = SectorWindTool()
    assert json.loads(t.execute(action="x"))["ok"] is False
    assert json.loads(t.execute(action="wind", horizon=99))["ok"] is False
    assert json.loads(t.execute(action="wind", keys=["nope"]))["ok"] is False


def test_dispatch(monkeypatch):
    import src.sector_pick.wind_scan as ws

    monkeypatch.setattr(ws, "run_wind_scan", lambda keys, horizon=5: {"robot": {"status": "ok"}})
    monkeypatch.setattr(ws, "run_wind_cards", lambda keys, horizon=5: [{"sector": "robot", "cards": []}])
    t = SectorWindTool()
    assert json.loads(t.execute(action="wind", keys=["robot"]))["data"]["robot"]["status"] == "ok"
    assert json.loads(t.execute(action="cards"))["data"][0]["sector"] == "robot"


def test_auto_registered():
    from src.tools import _discover_subclasses

    assert "sector_wind" in {c.name for c in _discover_subclasses()}
