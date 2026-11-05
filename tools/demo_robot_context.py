"""Create a new allowlisted robot build folder. Never upload the whole repository.

python tools/demo_robot_context.py --output <new-folder>
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
HOSPITAL = ("__init__", "browser", "dates", "firewall", "ledger", "pdfcapture", "pdfcheck", "runlog",
            "runner", "session", "showme", "skills", "targets", "watch", "brain", "explore", "learn", "skill_notes", "demo_tasks", "demo_record", "demo_site", "demo_robot")
CHAIN = ("__init__", "model", "docstore", "record", "files", "merge", "normalize", "today", "alerts", "agent_tasks", "agent_words", "auth", "settings")
FILES = ["wardos/__init__.py", "wardos/config.py", "wardos/models.py", "wardos/record/__init__.py", "wardos/record/labs.py",
         "wardos/hospital/portals/base.py", "wardos/hospital/portals/openmrs.py",
         "tools/openmrs_seed.py", "tools/openmrs_watch.py"]
FILES += [f"wardos/hospital/{name}.py" for name in HOSPITAL]
FILES += [f"wardos/chain/{name}.py" for name in CHAIN]
PORTAL_INIT = '''"""Cloud robot: OpenMRS is the only installed portal."""
from .base import Health, LoginError, PortalProfile
def get_portal(name, **overrides):
    if name != "openmrs" or overrides:
        raise ValueError("Only the fixed public OpenMRS demo is installed")
    from .openmrs import OpenMRSPortal
    portal = OpenMRSPortal()
    return portal
'''


def build_context(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in FILES:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    for path in (ROOT / "skills" / "openmrs").glob("*.json"):
        target = output / "skills" / "openmrs" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    (output / "wardos/hospital/portals/__init__.py").write_text(PORTAL_INIT, encoding="utf-8")
    for name in ("Dockerfile", "requirements.txt", "cloudbuild.yaml", ".gcloudignore", ".dockerignore"):
        shutil.copyfile(ROOT / "cloud" / "demo-robot" / name, output / name)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(build_context(parser.parse_args().output))
