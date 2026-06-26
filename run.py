#!/usr/bin/env python3
"""
Run both the scraper and dashboard server in one command.
Usage: python run.py
"""
import subprocess, sys, os, signal
from pathlib import Path

DIR = Path(__file__).parent

def main():
    procs = []
    try:
        # Start dashboard server
        srv = subprocess.Popen([sys.executable, DIR / "server.py"],
                               cwd=DIR, stdout=sys.stdout, stderr=sys.stderr)
        procs.append(srv)
        print(f"✓ Dashboard running at http://localhost:8080")

        # Start scraper (continuous mode)
        scr = subprocess.Popen([sys.executable, DIR / "scraper.py"],
                               cwd=DIR, stdout=sys.stdout, stderr=sys.stderr)
        procs.append(scr)
        print(f"✓ Scraper running (continuous mode)")
        print(f"  Press Ctrl+C to stop both\n")

        # Wait for either to exit
        scr.wait()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for p in procs:
            try: p.terminate()
            except: pass

if __name__ == "__main__":
    main()
