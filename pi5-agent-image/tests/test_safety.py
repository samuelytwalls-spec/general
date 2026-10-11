import pytest

from piagent.safety import CONFIRM, DENY, SAFE, classify

CASES = [
    ("ls -la /srv/docs", SAFE),
    ("df -h", SAFE),
    ("cat /etc/os-release | grep PRETTY", SAFE),
    ("uptime && free -m", SAFE),
    ("vcgencmd measure_temp", SAFE),
    ("ls 2>/dev/null", SAFE),
    ("ls 2>&1 | head", SAFE),
    ("find . -name x -exec ls {} ;", SAFE),
    ('bash -c "ls -l"', SAFE),
    ("python3 --version", SAFE),
    ("systemctl status ssh", SAFE),
    ("apt list --installed", SAFE),
    ("git status", SAFE),
    ("git branch -a", SAFE),
    ("nmcli dev wifi list", SAFE),
    ("ip addr show", SAFE),
    ("tar tzf a.tgz", SAFE),
    ("env FOO=1 ls", SAFE),
    ("curl -s http://example.com", SAFE),
    ("sed -n 1,5p f", SAFE),
    ("ps aux | sort -k3 -nr | head", SAFE),
    ("journalctl -u ssh -n 50", SAFE),
    ("crontab -l", SAFE),
    ("ln -s a b", SAFE),
    ("rm notes.txt", CONFIRM),
    ("echo hi > /tmp/x", CONFIRM),
    ("echo hi >> log.txt", CONFIRM),
    ("cat <<EOF > file", CONFIRM),
    ("dd if=a of=b", CONFIRM),
    ('find . -name "*.tmp" -delete', CONFIRM),
    ("find . -name x -exec rm {} ;", CONFIRM),
    ('bash -c "rm -rf x"', CONFIRM),
    ('python3 -c "import os"', CONFIRM),
    ("systemctl restart ssh", CONFIRM),
    ("apt install vim", CONFIRM),
    ("git reset --hard", CONFIRM),
    ("nmcli con down eth0", CONFIRM),
    ("ip link set eth0 down", CONFIRM),
    ("tar xzf a.tgz", CONFIRM),
    ("tar -czf out.tgz dir", CONFIRM),
    ("echo $(rm x)", CONFIRM),
    ("nohup rm x &", CONFIRM),
    ("timeout 5 rm -rf build", CONFIRM),
    ("xargs rm < files.txt", CONFIRM),
    ("ls; rm a", CONFIRM),
    ("ls\nrm a", CONFIRM),
    ("curl -o f http://x", CONFIRM),
    ("sed -i s/a/b/ f", CONFIRM),
    ("kill 1234", CONFIRM),
    ('echo "unterminated', CONFIRM),
    ("crontab -r", CONFIRM),
    ("ln -sf a b", CONFIRM),
    ("$CMD foo", CONFIRM),
    ("mv a b", CONFIRM),
    ("cp a b", CONFIRM),
    ("chmod 600 f", CONFIRM),
    ("rm -rf /", DENY),
    ("rm -rf /*", DENY),
    ("rm -fr ~", DENY),
    ("rm -r --no-preserve-root /x", DENY),
    ("sudo apt update", DENY),
    ("su -c id", DENY),
    ("dd if=/dev/zero of=/dev/mmcblk0 bs=1M", DENY),
    ("echo x > /dev/sda", DENY),
    (":(){ :|:& };:", DENY),
    ("mkfs.ext4 /dev/sda1", DENY),
    ("chmod -R 777 /", DENY),
    ("ls && sudo reboot", DENY),
    ("", DENY),
]


@pytest.mark.parametrize("command,level", CASES)
def test_classify(command, level):
    verdict = classify(command)
    assert verdict.level == level, (command, verdict.reasons)
    if level != SAFE:
        assert verdict.reasons


def test_extra_patterns():
    assert classify("vcgencmd get_throttled", extra_confirm=[r"vcgencmd"]).level == CONFIRM
    assert classify("curl http://x", extra_deny=[r"^curl\b"]).level == DENY
