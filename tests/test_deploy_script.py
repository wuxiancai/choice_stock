from pathlib import Path


DEPLOY = Path(__file__).parents[1] / "deploy.sh"
START = Path(__file__).parents[1] / "start.sh"


def test_deploy_script_installs_python_312_on_macos_and_ubuntu_when_missing():
    script = DEPLOY.read_text()
    assert "install_python_312" in script
    assert "brew install python@3.12" in script
    assert "apt-get install -y python3.12 python3.12-venv" in script


def test_start_script_braces_log_file_before_chinese_punctuation():
    script = START.read_text()
    assert "${LOG_FILE}）" in script
