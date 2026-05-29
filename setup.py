from setuptools import setup, find_packages

setup(
    name="backupme",
    version="2.0.0",
    packages=find_packages(),
    install_requires=["click>=8.1", "pyyaml>=6.0"],
    entry_points={"console_scripts": ["backupme=backupme.cli.base:cli"]},
)