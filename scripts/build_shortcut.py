#!/usr/bin/env python3
"""Write the "Morning Brief" share-sheet Shortcut as an unsigned plist.

The token is NOT in the file: an import question asks for it when you add the Shortcut. Sign the output on a Mac
before sharing it to a phone:  shortcuts sign --mode anyone --input "Morning Brief.shortcut" --output signed.shortcut
"""
from __future__ import annotations

import argparse
import plistlib
from pathlib import Path
from urllib.parse import urlsplit

OBJ = "￼"  # Shortcuts' placeholder character for an inline variable
U_TOKEN, U_INPUT, U_NOTE = "6A0D1E10-0001-4A00-8000-000000000001", "6A0D1E10-0002-4A00-8000-000000000002", \
    "6A0D1E10-0003-4A00-8000-000000000003"
U_TOP, U_END, U_REQ, U_MSG = "6A0D1E10-0004-4A00-8000-000000000004", "6A0D1E10-0005-4A00-8000-000000000005", \
    "6A0D1E10-0006-4A00-8000-000000000006", "6A0D1E10-0007-4A00-8000-000000000007"
MENU_GROUP, MENU_END = "6A0D1E10-0008-4A00-8000-000000000008", "6A0D1E10-0009-4A00-8000-000000000009"


def text(value: str) -> dict:
    return {"Value": {"string": value, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def with_var(template: str, attachment: dict) -> dict:
    """A text field containing one inline variable where OBJ appears."""
    at = template.index(OBJ)
    return {"Value": {"string": template, "attachmentsByRange": {f"{{{at}, 1}}": attachment}},
            "WFSerializationType": "WFTextTokenString"}


def output(uuid: str, name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": uuid, "OutputName": name}


def variable(name: str) -> dict:
    return {"Type": "Variable", "VariableName": name}


def attachment(ref: dict) -> dict:
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def fields(pairs: list[tuple[str, dict]]) -> dict:
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": text(key), "WFValue": value} for key, value in pairs]},
        "WFSerializationType": "WFDictionaryFieldValue"}


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": f"is.workflow.actions.{identifier}", "WFWorkflowActionParameters": params}


def build(server: str) -> dict:
    url = server.rstrip("/") + "/api/inbox"
    actions = [
        action("gettext", UUID=U_TOKEN, WFTextActionText=""),  # filled in by the import question
        action("setvariable", WFVariableName="Token", WFInput=attachment(output(U_TOKEN, "Text"))),
        action("gettext", UUID=U_INPUT, WFTextActionText=with_var(OBJ, {"Type": "ExtensionInput"})),
        action("ask", UUID=U_NOTE, WFAskActionPrompt="Anything to focus on? (optional)", WFInputType="Text"),
        action("choosefrommenu", GroupingIdentifier=MENU_GROUP, WFControlFlowMode=0,
               WFMenuPrompt="Where in the queue?", WFMenuItems=["Top of queue", "End of queue"]),
        action("choosefrommenu", GroupingIdentifier=MENU_GROUP, WFControlFlowMode=1, WFMenuItemTitle="Top of queue"),
        action("gettext", UUID=U_TOP, WFTextActionText="top"),
        action("setvariable", WFVariableName="Position", WFInput=attachment(output(U_TOP, "Text"))),
        action("choosefrommenu", GroupingIdentifier=MENU_GROUP, WFControlFlowMode=1, WFMenuItemTitle="End of queue"),
        action("gettext", UUID=U_END, WFTextActionText="end"),
        action("setvariable", WFVariableName="Position", WFInput=attachment(output(U_END, "Text"))),
        action("choosefrommenu", GroupingIdentifier=MENU_GROUP, WFControlFlowMode=2, UUID=MENU_END),
        action("downloadurl", UUID=U_REQ, WFURL=url, WFHTTPMethod="POST", WFHTTPBodyType="JSON", ShowHeaders=True,
               WFHTTPHeaders=fields([("Authorization", with_var(f"Bearer {OBJ}", variable("Token")))]),
               WFJSONValues=fields([
                   ("input", with_var(OBJ, output(U_INPUT, "Text"))),
                   ("note", with_var(OBJ, output(U_NOTE, "Provided Input"))),
                   ("position", with_var(OBJ, variable("Position"))),
               ])),
        action("getvalueforkey", UUID=U_MSG, WFInput=attachment(output(U_REQ, "Contents of URL")),
               WFDictionaryKey="message"),
        action("notification", WFNotificationActionTitle="Morning Brief",
               WFNotificationActionBody=with_var(OBJ, output(U_MSG, "Dictionary Value"))),
    ]
    return {
        "WFWorkflowClientVersion": "2605.0.5",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4274264319, "WFWorkflowIconGlyphNumber": 59771},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFStringContentItem"],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorAskForInput",
                                      "Parameters": {"ItemClass": "WFStringContentItem"}},
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowImportQuestions": [{
            "ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText", "DefaultValue": "",
            "Text": "Paste your INBOX_TOKEN (from the server's .env)",
        }],
        "WFWorkflowActions": actions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", required=True, help="e.g. http://100.x.y.z:8430 (your tailnet address)")
    parser.add_argument("--out", default="Morning Brief.shortcut")
    args = parser.parse_args()
    parts = urlsplit(args.server)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        parser.error("--server must be a full http(s) URL, e.g. http://100.x.y.z:8430")
    Path(args.out).write_bytes(plistlib.dumps(build(args.server), fmt=plistlib.FMT_BINARY))
    print(f"wrote {args.out}. Sign it on a Mac: shortcuts sign --mode anyone --input \"{args.out}\" "
          f"--output \"Morning Brief (signed).shortcut\"")


if __name__ == "__main__":
    main()
