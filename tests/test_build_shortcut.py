import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_shortcut.py"


def load():
    spec = importlib.util.spec_from_file_location("build_shortcut", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def action_ids(workflow):
    return [a["WFWorkflowActionIdentifier"] for a in workflow["WFWorkflowActions"]]


def test_build_structure():
    wf = load().build("http://10.0.0.5:8430/")
    assert wf["WFWorkflowTypes"] == ["ActionExtension"]
    assert set(wf["WFWorkflowInputContentItemClasses"]) == {"WFURLContentItem", "WFStringContentItem"}
    ids = action_ids(wf)
    assert ids[0] == "is.workflow.actions.gettext"
    assert "is.workflow.actions.ask" in ids and "is.workflow.actions.choosefrommenu" in ids
    assert "is.workflow.actions.downloadurl" in ids and ids[-1] == "is.workflow.actions.notification"
    (question,) = wf["WFWorkflowImportQuestions"]
    assert question["ActionIndex"] == 0 and question["ParameterKey"] == "WFTextActionText"
    assert "INBOX_TOKEN" in question["Text"]
    request = next(a for a in wf["WFWorkflowActions"]
                   if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl")
    params = request["WFWorkflowActionParameters"]
    assert params["WFURL"] == "http://10.0.0.5:8430/api/inbox"
    assert params["WFHTTPMethod"] == "POST" and params["WFHTTPBodyType"] == "JSON"
    keys = [item["WFKey"]["Value"]["string"]
            for item in params["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]]
    assert keys == ["input", "note", "position", "date"]
    menu_titles = [a["WFWorkflowActionParameters"].get("WFMenuItemTitle") for a in wf["WFWorkflowActions"]
                   if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.choosefrommenu"]
    assert {"Top of queue", "End of queue", "Tomorrow's brief", "Pick a day…"} <= set(menu_titles)
    ids = action_ids(wf)
    assert "is.workflow.actions.format.date" in ids
    fmt = next(a for a in wf["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.format.date")
    assert fmt["WFWorkflowActionParameters"]["WFDateFormat"] == "yyyy-MM-dd"
    asks = [a["WFWorkflowActionParameters"] for a in wf["WFWorkflowActions"]
            if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.ask"]
    assert any(p.get("WFInputType") == "Date" for p in asks)
    menu = next(a for a in wf["WFWorkflowActions"] if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.choosefrommenu")
    assert menu["WFWorkflowActionParameters"]["WFMenuItems"] == ["Top of queue", "End of queue", "Tomorrow's brief",
                                                                 "Pick a day…"]


def test_cli_rejects_a_bad_server_url(tmp_path):
    out = tmp_path / "Morning Brief.shortcut"
    result = subprocess.run([sys.executable, str(SCRIPT), "--server", "nope", "--out", str(out)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not out.exists()


def test_cli_writes_binary_plist_without_a_token(tmp_path):
    out = tmp_path / "Morning Brief.shortcut"
    subprocess.run([sys.executable, str(SCRIPT), "--server", "http://10.0.0.5:8430", "--out", str(out)],
                   check=True, capture_output=True)
    raw = out.read_bytes()
    assert raw.startswith(b"bplist00")
    wf = plistlib.loads(raw)
    assert wf["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["WFTextActionText"] == ""
    request = next(a for a in wf["WFWorkflowActions"]
                   if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl")
    (header,) = request["WFWorkflowActionParameters"]["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    # the header is a template: "Bearer " + the Token variable, filled in from the import question
    assert header["WFValue"]["Value"]["string"] == "Bearer ￼"
    assert header["WFValue"]["Value"]["attachmentsByRange"] == {"{7, 1}": {"Type": "Variable", "VariableName": "Token"}}
