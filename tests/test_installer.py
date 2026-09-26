import os
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("version", "supported"), [
    ("3.11", False),
    ("3.12", True),
    ("3.13", True),
    ("3.14", True),
])
def test_installer_python_version_check(tmp_path, version, supported):
    bash = shutil.which("bash")
    if os.name != "posix" or not bash:
        pytest.skip("installer version check requires bash on POSIX")

    interpreter = tmp_path / "mock-python"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "version = tuple(map(int, os.environ['MOCK_PYTHON_VERSION'].split('.')))\n"
        "code = sys.argv[2].replace('sys.version_info', repr(version))\n"
        "exec(code)\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    helper = os.path.join(os.path.dirname(__file__), "..", "scripts", "python-version.sh")
    result = subprocess.run(
        [bash, "-c", 'source "$1"; python_version_supported "$2"', "test", helper, str(interpreter)],
        env={**os.environ, "MOCK_PYTHON_VERSION": version},
        check=False,
    )

    assert (result.returncode == 0) is supported
