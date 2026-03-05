from pathlib import Path

from utils.encoding_health import scan_project


def test_repository_has_no_encoding_health_issues():
    issues = scan_project(Path.cwd())
    assert not issues, "\n".join(issue.render(Path.cwd()) for issue in issues[:30])
