"""An app update must not relocate a registered Python used by other apps."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PWSH = ROOT / ".tools/desktop/powershell/pwsh.exe"
if not PWSH.is_file():
    PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PWSH, reason="PowerShell 7 required")


def selection(tmp_path, *, compatible, registered):
    helper = ROOT / "scripts/release/python-discovery.ps1"
    script = tmp_path / "probe.ps1"
    script.write_text("\n".join([
        "$ErrorActionPreference='Stop'",
        ". '" + str(helper).replace("'", "''") + "'",
        "function Test-Quest3DPythonRuntime([string]$Candidate) { return " +
        ("($Candidate -eq 'compatible.exe')" if compatible else "$false") + " }",
        "try {",
        "  $result = Select-Quest3DPythonRuntime @('missing.exe', 'compatible.exe') " +
        ("$true" if registered else "$false"),
        "  @{selected=$result.reused; install_allowed=$result.install_allowed} | ConvertTo-Json -Compress",
        "} catch { @{blocked=$true} | ConvertTo-Json -Compress }",
    ]), "utf-8")
    result = subprocess.run([str(PWSH), "-NoProfile", "-File", str(script)],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_registered_compatible_runtime_is_reused_without_install(tmp_path):
    assert selection(tmp_path, compatible=True, registered=True) == {
        "selected": True, "install_allowed": False}


def test_broken_or_incompatible_registration_blocks_relocation(tmp_path):
    assert selection(tmp_path, compatible=False, registered=True) == {"blocked": True}


def test_fresh_machine_can_install_pinned_runtime(tmp_path):
    assert selection(tmp_path, compatible=False, registered=False) == {
        "selected": False, "install_allowed": True}
