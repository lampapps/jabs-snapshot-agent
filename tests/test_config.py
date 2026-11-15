import re
from pathlib import Path

import pytest
import yaml

from app import config
from app.config import ConfigError

from .conftest import write_config, write_job


def test_defaults_need_only_a_repo_path(home):
    write_config(home)
    cfg = config.load_config()
    assert cfg.repo_path == "/tmp/repo"
    assert cfg.retention.keep_daily == 0 and cfg.restic_options.exclude_caches is True
    assert cfg.email.notify_on.error.enabled is True and cfg.email.notify_on.backup_complete.enabled is False
    assert cfg.dashboard.enabled is True and cfg.storage.source_size is True


def test_blank_yaml_keys_use_defaults(home):
    write_config(home, "repo_path: /r\nretention:\nemail:\n  to_addrs:\nrestic_options:\n  host:\n")
    cfg = config.load_config()
    assert cfg.retention.keep_daily == 0 and cfg.email.to_addrs == [] and cfg.restic_options.host == ""


def test_the_old_global_yaml_example_still_validates(home):
    write_config(home, """
repo_path: /mnt/nas/repo
retention: {keep_daily: 7, keep_weekly: 4, keep_within: "", group_by: "", extra_forget_args: []}
restic_options: {limit_upload: 0, compression: "", retry_lock: "5m", exclude_caches: true, exclude_if_present: [], host: ""}
use_common_exclude: true
verify_after_backup: true
email:
  smtp_server: smtp.zoho.com
  smtp_port: 587
  to_addrs: [admin@example.com]
  use_tls: true
  notify_on: {error: {enabled: true}, backup_complete: {enabled: false}}
""")
    assert config.load_config().email.to_addrs == ["admin@example.com"]


@pytest.mark.parametrize("text,where", [
    ("repo_path: /r\nretention: {keep_daily: -1}\n", "retention.keep_daily"),
    ("repo_path: /r\nrestic_options: {compression: zip}\n", "restic_options.compression"),
    ("repo_path: /r\nunknown_key: 1\n", "unknown_key"),
    ("repo_path: /r\nhostname: 'two words'\n", "hostname"),
    ("repo_path: /r\nemail: {to_addrs: [nope]}\n", "email.to_addrs"),
    ("repo_path: /r\nstorage: {volume_interval_minutes: 0}\n", "storage.volume_interval_minutes"),
])
def test_invalid_config_names_the_key(home, text, where):
    write_config(home, text)
    with pytest.raises(ConfigError, match=where):
        config.load_config()


def test_missing_config_says_what_to_do(home):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        config.load_config()
    (home / "config" / "global.yaml").write_text("repo_path: /r\n")
    with pytest.raises(ConfigError, match="global.yaml"):
        config.load_config()


def test_bad_yaml_and_wrong_top_level(home):
    write_config(home, "repo_path: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        config.load_config()
    write_config(home, "- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        config.load_config()


class TestJobs:
    def load(self, home, body="", cfg_text="repo_path: /tmp/repo\n"):
        write_config(home, cfg_text)
        path = write_job(home, "jim-home", body)
        return config.load_job(path, config.load_config())

    def test_job_inherits_the_global_settings(self, home):
        job = self.load(home, cfg_text="repo_path: /r\nverify_after_backup: false\nretention: {keep_daily: 7}\n")
        assert (job.name, job.repo_path, job.verify_after_backup, job.retention.keep_daily) == ("jim-home", "/r", False, 7)

    def test_name_defaults_to_the_file_name(self, home):
        job = self.load(home, "paths: [/data]\n")
        assert job.name == "jim-home" and job.tags == []

    def test_overrides_replace_only_the_keys_they_name(self, home):
        job = self.load(home, "paths: [/d]\nverify_after_backup: true\nretention: {keep_daily: 3}\n"
                              "restic_options: {limit_upload: 100}\nrepo_path: /other\n",
                        "repo_path: /r\nverify_after_backup: false\nretention: {keep_daily: 7, keep_weekly: 4}\n"
                        "restic_options: {compression: auto}\n")
        assert job.verify_after_backup is True and job.repo_path == "/other"
        assert (job.retention.keep_daily, job.retention.keep_weekly) == (3, 4)
        assert (job.restic_options.limit_upload, job.restic_options.compression) == (100, "auto")

    def test_blank_override_keeps_the_global_value(self, home):
        job = self.load(home, "paths: [/d]\nretention:\n  keep_daily:\n", "repo_path: /r\nretention: {keep_daily: 7}\n")
        assert job.retention.keep_daily == 7

    def test_common_excludes_come_first(self, home):
        (home / "config" / "common_exclude.yaml").write_text('- "*.tmp"\n- "node_modules/"\n')
        job = self.load(home, "paths: [/d]\nexclude: [Videos/]\n")
        assert job.exclude == ["*.tmp", "node_modules/", "Videos/"]
        job = self.load(home, "paths: [/d]\nexclude: [Videos/]\n", "repo_path: /r\nuse_common_exclude: false\n")
        assert job.exclude == ["Videos/"]

    def test_schedules(self, home):
        job = self.load(home, 'paths: [/d]\nschedules:\n  - {cron: "0 3 * * *", enabled: true}\n  - {cron: "0 4 * * 1"}\n')
        assert job.enabled_crons() == ["0 3 * * *"]

    @pytest.mark.parametrize("body,where", [
        ("paths: []\n", "paths"),
        ("tags: [a]\n", "paths"),
        ("paths: [/d]\nschedules: [{cron: 'not cron', enabled: true}]\n", "schedules"),
        ("paths: [/d]\nschedules: [{cron: '*/5 * * * * *'}]\n", "schedules"),
        ("paths: [/d]\nretention: {bogus: 1}\n", "retention"),
        ("paths: [/d]\nrestic_options: {compression: zip}\n", "restic_options"),
        ("paths: [/d]\nsurprise: 1\n", "surprise"),
    ])
    def test_invalid_job_files(self, home, body, where):
        with pytest.raises(ConfigError, match=where):
            self.load(home, body)

    def test_repo_path_is_required_somewhere(self, home):
        with pytest.raises(ConfigError, match="repo_path"):
            self.load(home, "paths: [/d]\n", "dashboard: {enabled: true}\n")

    def test_find_job_by_file_name_job_name_or_path(self, home):
        write_config(home)
        path = write_job(home, "file-name", 'job_name: "shown-name"\npaths: [/d]\n')
        cfg = config.load_config()
        assert config.find_job("file-name", cfg).name == "shown-name"
        assert config.find_job("shown-name", cfg).path == str(path)
        assert config.find_job(str(path), cfg).name == "shown-name"
        with pytest.raises(ConfigError, match="not found"):
            config.find_job("nope", cfg)

    def test_one_broken_file_does_not_hide_the_others(self, home):
        write_config(home)
        write_job(home, "good", "paths: [/d]\n")
        write_job(home, "bad", "paths: []\n")
        jobs, errors = config.load_jobs(config.load_config())
        assert [j.name for j in jobs] == ["good"] and len(errors) == 1 and "bad.yaml" in errors[0]

    def test_duplicate_job_names_are_reported(self, home):
        write_config(home)
        write_job(home, "a", 'job_name: same\npaths: [/d]\n')
        write_job(home, "b", 'job_name: same\npaths: [/d]\n')
        _, errors = config.load_jobs(config.load_config())
        assert any("same" in e and "more than one" in e for e in errors)


class TestJobTemplate:
    TEMPLATE = Path(__file__).resolve().parent.parent / "config" / "templates" / "job.yaml"

    def text(self) -> str:
        return self.TEMPLATE.read_text(encoding="utf-8")

    def uncommented(self, key: str) -> dict:
        """The commented-out `key:` block of the template, uncommented and parsed."""
        lines, found = [], False
        for line in self.text().splitlines():
            if re.match(rf"^# {key}:", line):
                found = True
                lines.append(line[2:])
            elif found and line.startswith("#   "):
                lines.append(line[2:])
            elif found:
                break
        assert found, f"{key} is not in the template"
        return yaml.safe_load("\n".join(lines))

    def test_the_template_is_a_valid_job(self, home):
        write_config(home)
        write_job(home, "example", self.text())
        job = config.find_job("example", config.load_config())
        assert job.name == "example" and job.tags == ["example"] and not job.enabled_crons()

    def test_every_setting_a_job_can_have_is_listed(self):
        text = self.text()
        names = set(config.JobFile.model_fields) | set(config.Retention.model_fields) | set(config.ResticOptions.model_fields)
        missing = sorted(name for name in names if not re.search(rf"^#? ?\s*{name}:", text, re.M))
        assert not missing

    def test_the_commented_overrides_are_valid_when_uncommented(self):
        merged = {}
        for key in ("retention", "restic_options", "exclude"):
            merged.update(self.uncommented(key))
        config.JobFile.model_validate({"paths": ["/p"], **merged})
