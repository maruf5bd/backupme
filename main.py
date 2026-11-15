import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from backupme.cli import cli

def main():
    cli()

if __name__ == "__main__":
    main()