#!/usr/bin/env python3
"""Install (or remove) the Finder Quick Actions for atem2fcp.

  Convert ATEM to FCPXML              writes <name>.fcpxml next to each selected .drp,
                                      using the options saved in the front end
  Convert ATEM to FCPXML with Options opens the front end with the selected .drp

Run it with the Python that should run the tool (it needs tkinter for the
front end). The Quick Actions point at the scripts in this folder, so run it
again if you move the folder.

Usage: python3 install_services.py [--uninstall]
"""

import os
import plistlib
import shlex
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.expanduser("~/Library/Services")
# macOS has no registered type for .drp, so Finder uses this dynamic UTI derived
# from the extension. It limits the menu items to .drp files.
DRP_UTI = "dyn.ah62d4rv4ge80k6xu"

CONVERT = "Convert ATEM to FCPXML"
OPTIONS = "Convert ATEM to FCPXML with Options…"

HEADER = """export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LC_ALL="en_US.UTF-8"
PY=@PY@
DIR=@DIR@
"""

CONVERT_SCRIPT = HEADER + r"""
notify() { /usr/bin/osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' "$@"; }
alert() { /usr/bin/osascript -e 'on run argv' -e 'display alert (item 1 of argv) message (item 2 of argv) as warning' -e 'end run' "$@" >/dev/null; }

ok=0; failed=0; problems=""; detail=""
for f in "$@"; do
  if [[ "${f:e:l}" != drp ]]; then
    problems+="${f:t}: not an ATEM .drp file"$'\n\n'; failed=$((failed + 1)); continue
  fi
  if out="$("$PY" "$DIR/atem2fcp.py" --use-settings "$f" 2>&1)"; then
    ok=$((ok + 1))
    detail="$(print -r -- "$out" | sed -n 's/^  Shots: *//p')"
    w="$(print -r -- "$out" | sed -nE 's/^  (Warning|Note): //p')"
    [[ -n "$w" ]] && problems+="${f:t:r}.fcpxml was created, but:"$'\n'"$w"$'\n\n'
  else
    failed=$((failed + 1))
    problems+="${f:t}: ${out#error: }"$'\n\n'
  fi
done

if [[ -n "$problems" ]]; then
  title="Converted $ok file(s)"; [[ $failed -gt 0 ]] && title="$title, $failed failed"
  alert "$title" "${problems%$'\n\n'}"
elif [[ $ok -eq 1 ]]; then
  notify "FCPXML created" "${f:t:r}.fcpxml — $detail"
elif [[ $ok -gt 1 ]]; then
  notify "FCPXML created" "$ok files converted"
fi
"""

OPTIONS_SCRIPT = HEADER + r"""
drp=""
for f in "$@"; do [[ "${f:e:l}" == drp ]] && { drp="$f"; break; }; done
if [[ -n "$drp" ]]; then
  nohup "$PY" "$DIR/atem2fcp_gui.py" "$drp" >/dev/null 2>&1 &
else
  nohup "$PY" "$DIR/atem2fcp_gui.py" >/dev/null 2>&1 &
fi
"""


def workflow_plists(name, script):
    info = {"NSServices": [{
        "NSMenuItem": {"default": name},
        "NSMessage": "runWorkflowAsService",
        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
        "NSSendFileTypes": [DRP_UTI],
    }]}
    ids = [str(uuid.uuid4()).upper() for _ in range(3)]
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.path"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in
                                  ("COMMAND_STRING", "CheckedForUserDefaultShell", "inputMethod", "shell", "source")},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.path"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {
            "COMMAND_STRING": script,
            "CheckedForUserDefaultShell": True,
            "inputMethod": 1,  # pass input as arguments
            "shell": "/bin/zsh",
            "source": "",
        },
        "BundleIdentifier": "com.apple.Automator.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": ids[0],
        "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
        "OutputUUID": ids[1],
        "UUID": ids[2],
        "UnlocalizedApplications": ["Automator"],
        "arguments": {},
        "isViewVisible": 1,
        "location": "309.000000:253.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
    }
    document = {
        "AMApplicationBuild": "521",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 11,
            "processesInput": 0,
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    return info, document


def install(name, script):
    contents = os.path.join(SERVICES, f"{name}.workflow", "Contents")
    os.makedirs(contents, exist_ok=True)
    info, document = workflow_plists(name, script)
    with open(os.path.join(contents, "Info.plist"), "wb") as fh:
        plistlib.dump(info, fh)
    with open(os.path.join(contents, "document.wflow"), "wb") as fh:
        plistlib.dump(document, fh)
    print(f"Installed  ~/Library/Services/{name}.workflow")


def refresh_services():
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], check=False)


def main():
    if "--uninstall" in sys.argv[1:]:
        import shutil
        for name in (CONVERT, OPTIONS):
            path = os.path.join(SERVICES, f"{name}.workflow")
            if os.path.isdir(path):
                shutil.rmtree(path)
                print(f"Removed    ~/Library/Services/{name}.workflow")
        refresh_services()
        return 0

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(f"warning: {sys.executable} has no tkinter, so the front end won't open. "
              "Run this installer with a Python that has it (e.g. from python.org).", file=sys.stderr)
    def fill(script):
        return script.replace("@PY@", shlex.quote(sys.executable)).replace("@DIR@", shlex.quote(HERE))
    install(CONVERT, fill(CONVERT_SCRIPT))
    install(OPTIONS, fill(OPTIONS_SCRIPT))
    refresh_services()
    print(f"Python:    {sys.executable}")
    print(f"Scripts:   {HERE}")
    print("Right-click a .drp in Finder → Quick Actions (or Services) to use them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
