from typer.testing import CliRunner
from haydar.cli import app

runner = CliRunner()

def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Haydar v" in result.stdout

def test_uninstall_dry_run():
    result = runner.invoke(app, ["uninstall", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY RUN - No files will be deleted." in result.stdout
