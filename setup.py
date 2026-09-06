from setuptools import setup, find_packages

setup(
    name="backupme",
    version="2.0.0",
    packages=find_packages(),
    install_requires=[
        "click>=8.1",
        "pyyaml>=6.0",
        "paramiko>=3.0",
        "smbclient>=0.5",
        "cryptography>=41.0",
        "argon2-cffi>=23.1",
        "zstandard>=0.22",
        "fastapi>=0.100",
        "uvicorn>=0.22",
        "jinja2>=3.1",
        "watchfiles>=0.19",
        "httpx>=0.24",
        "pydantic>=2.0",
    ],
    entry_points={"console_scripts": ["backupme=backupme.cli.base:cli"]},
)