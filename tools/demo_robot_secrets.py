"""Approved AM deployment only: transfer two secrets without printing/writing their contents.

The Meta key is read directly from the Windows user variable, never a local file.
Cloud OAuth tokens are captured in memory, never returned on stdout.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import winreg

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]


def seed_payload(people):
    payload = json.dumps(people, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > 65536:
        raise ValueError("Compact demo seed exceeds Secret Manager's 64 KiB limit")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved", choices=["yes"], required=True)
    parser.add_argument("--only", choices=["seed", "meta", "both"], default="both")
    args = parser.parse_args()
    from wardos.hospital.demo_site import load_people
    import requests
    path = ROOT / "private" / "openmrs" / "patients.json"
    load_people(path)        # checks the file; the robot does its own load, so upload the file as written
    people = json.loads(path.read_text(encoding="utf-8"))
    payloads = []
    if args.only in {"seed", "both"}:
        payloads.append(("wardos-demo-seed", seed_payload(people)))
    if args.only in {"meta", "both"}:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            meta = winreg.QueryValueEx(handle, "MODEL_API_KEY")[0].encode("utf-8")
        payloads.append(("wardos-demo-meta-key", meta))
    command = subprocess.run([r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
                              "auth", "print-access-token", "--project=your-gcp-project"], capture_output=True, text=True)
    if command.returncode:
        raise RuntimeError("Cloud CLI authentication failed")
    token = command.stdout.strip()
    for name, payload in payloads:
        reply = requests.post(f"https://secretmanager.googleapis.com/v1/projects/your-gcp-project/secrets/{name}:addVersion",
                              headers={"Authorization": "Bearer " + token},
                              json={"payload": {"data": base64.b64encode(payload).decode("ascii")}}, timeout=30)
        if reply.status_code >= 400:
            raise RuntimeError(f"Secret upload failed for {name} (HTTP {reply.status_code})")
        print(name + ": version added")


if __name__ == "__main__":
    main()
