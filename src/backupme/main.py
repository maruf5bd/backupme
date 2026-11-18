"""Package entry point: runs the backupme CLI when executed as a module (python -m backupme).
"""
from .cli.base import cli

if __name__ == "__main__":
    cli()