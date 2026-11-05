"""python -m wardos.agent.probe: ONE eHospital login try, then leave. Prints only whether it worked."""
from __future__ import annotations

import asyncio
import sys


async def probe() -> int:
    from .hands import EHospitalHands
    hands = EHospitalHands(headless=True)
    try:
        logins = await hands.visit()
    except Exception as error:
        print(f"PROBE: login did not finish ({type(error).__name__}: {str(error)[:160]})", flush=True)
        return 1
    print(f"PROBE: signed in to eHospital ({logins} login{'' if logins == 1 else 's'} used)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(probe()))
