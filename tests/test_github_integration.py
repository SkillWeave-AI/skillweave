"""Tests for github_integration subpackage (Initiative 06).

Covers: inventory, autotag, changelog, issue_manager,
pr_description, docs_sync, release_gate, release_notes.
"""

import os
import sys
import json
import tempfile
import shutil
import re
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skillweave.github_integration.inventory import WorkflowInventory, WorkflowInfo, InventoryResult
from skillweave.github_integration.autotag import AutoTagger, TagCandidate, AutoTagResult
from skillweave.github_integration.capability_sync import CapaciumManifestSync
from skillweave.github_integration.changelog import ChangelogGenerator, ChangelogResult, CommitEntry
from skillweave.github_integration.issue_manager import IssueManager, IssueManagerResult, IssueSuggestion
from skillweave.github_integration.pr_description import PRDescriptionGenerator, PRDescriptionResult
from skillweave.github_integration.docs_sync import DocsSynchronizer, DocsSyncResult
from skillweave.github_integration.release_gate import ReleaseReadinessGate, ReleaseGateResult, GateCheck
from skillweave.github_integration.release_notes import ReleaseNotesGenerator


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def temp_repo():
    root = Path(tempfile.mkdtemp())
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text(
        "name: Test Workflow\non:\n  push:\n    branches: [main]\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
    )
    (workflows / "deploy.yml").write_text(
        "name: Deploy\non:\n  release:\n    types: [published]\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo deploy\n"
    )
    yield root
    shutil.rmtree(str(root))


@pytest.fixture
def temp_pyproject():
    root = Path(tempfile.mkdtemp())
    (root / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (root / "src" / "skillweave").mkdir(parents=True)
    yield root
    shutil.rmtree(str(root))


@pytest.fixture
def temp_project_full():
    root = Path(tempfile.mkdtemp())
    (root / "src" / "app").mkdir(parents=True)
    (root / "skills" / "skillweave-blueprint").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_pass(): assert True")
    (root / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.2.0] - 2026-04-27\n### Added\n- Foo\n")
    (root / "README.md").write_text("# App")
    (root / "LICENSE").write_text("Apache-2.0")
    (root / "capability.yaml").write_text(
        "kind: bundle\n"
        "name: skillweave\n"
        "version: 0.2.0\n"
        "description: temp bundle\n"
        "author: SkillWeave Team\n"
        "license: Apache-2.0\n"
        "owner: SkillWeave-AI\n"
        "repository: https://github.com/SkillWeave-AI/skillweave\n"
        "homepage: https://github.com/SkillWeave-AI/skillweave\n"
        "frameworks:\n"
        "  - opencode\n"
        "  - claude-code\n"
        "  - gemini-cli\n"
        "capabilities:\n"
        "  - name: skillweave-blueprint\n"
        "    source: ./skills/skillweave-blueprint\n"
        "    version: 0.2.0\n"
    )
    (root / "skills" / "skillweave-blueprint" / "capability.yaml").write_text(
        "kind: skill\n"
        "name: skillweave-blueprint\n"
        "version: 0.2.0\n"
        "description: temp skill\n"
        "author: SkillWeave Team\n"
        "license: Apache-2.0\n"
        "owner: SkillWeave-AI\n"
        "repository: https://github.com/SkillWeave-AI/skillweave\n"
        "homepage: https://github.com/SkillWeave-AI/skillweave\n"
        "frameworks:\n"
        "  - opencode\n"
        "  - claude-code\n"
        "  - gemini-cli\n"
        "keywords:\n"
        "  - prd\n"
    )
    CapaciumManifestSync(repo_root=str(root)).write()
    yield root
    shutil.rmtree(str(root))


# ─── Inventory Tests ───────────────────────────────────────────────────


class TestWorkflowInventory:
    def test_inventories_workflows(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        assert result.total_workflows == 2
        assert len(result.workflows) == 2

    def test_parses_workflow_info(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        names = {w.name for w in result.workflows}
        assert "Test Workflow" in names
        assert "Deploy" in names

    def test_detects_triggers(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        assert "push" in result.trigger_summary
        assert "release" in result.trigger_summary

    def test_parses_jobs(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        for wf in result.workflows:
            assert len(wf.jobs) > 0

    def test_detects_permissions(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        deploy = [w for w in result.workflows if w.name == "Deploy"]
        assert deploy
        assert deploy[0].permissions == {"contents": "write"}

    def test_generates_markdown(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        md = inv.generate_markdown(result)
        assert "Workflow Inventory" in md
        assert "Test Workflow" in md

    def test_generates_json(self, temp_repo):
        inv = WorkflowInventory(repo_root=str(temp_repo))
        result = inv.inventory()
        data = json.loads(inv.generate_json(result))
        assert data["total_workflows"] == 2

    def test_no_workflows_dir(self):
        inv = WorkflowInventory(repo_root="/tmp/nonexistent_dir_xyz")
        result = inv.inventory()
        assert result.total_workflows == 0
        assert len(result.errors) > 0


# ─── AutoTagger Tests ──────────────────────────────────────────────────


class TestAutoTagger:
    def test_detects_version(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        version = tagger.get_current_version()
        assert version == "1.0.0"

    def test_detects_no_version(self):
        root = Path(tempfile.mkdtemp())
        tagger = AutoTagger(repo_root=str(root))
        assert tagger.get_current_version() is None
        shutil.rmtree(str(root))

    def test_analyze_new_version(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        result = tagger.analyze(existing_tags=["v0.9.0"])
        assert result.should_release is True
        assert result.current_version == "1.0.0"

    def test_analyze_same_version(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        result = tagger.analyze(existing_tags=["v1.0.0"])
        assert result.should_release is False

    def test_analyze_no_tags(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        result = tagger.analyze(existing_tags=[])
        assert result.should_release is True
        assert result.latest_tag is None

    def test_tag_commands(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        result = tagger.analyze(existing_tags=["v0.9.0"])
        cmds = tagger.generate_tag_commands(result)
        assert len(cmds) == 1
        assert "git tag -a v1.0.0" in cmds[0]

    def test_tag_commands_no_new(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        result = tagger.analyze(existing_tags=["v1.0.0"])
        cmds = tagger.generate_tag_commands(result)
        assert cmds[0].startswith("# No new tags")

    def test_generates_json(self, temp_pyproject):
        tagger = AutoTagger(repo_root=str(temp_pyproject))
        result = tagger.analyze(existing_tags=["v0.9.0"])
        data = json.loads(tagger.generate_json(result))
        assert data["current_version"] == "1.0.0"
        assert data["should_release"] is True


# ─── Capacium Sync Tests ───────────────────────────────────────────────


class TestCapaciumManifestSync:
    def test_bundle_bump_preserves_decoupled_member_versions(self, temp_project_full):
        pyproject = temp_project_full / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('version = "0.2.0"', 'version = "0.3.0"'))

        syncer = CapaciumManifestSync(repo_root=str(temp_project_full))
        issues = syncer.write()

        assert issues
        assert syncer.check() == []
        root = syncer.load_manifest(temp_project_full / "capability.yaml")
        member = syncer.load_manifest(
            temp_project_full / "skills" / "skillweave-blueprint" / "capability.yaml"
        )
        assert root["version"] == "0.3.0"
        assert member["version"] == "0.2.0"
        assert root["capabilities"][0]["version"] == member["version"]

    def test_check_detects_drift(self, temp_project_full):
        mismatch = (temp_project_full / "skills" / "skillweave-blueprint" / "capability.yaml").read_text().replace(
            "version: 0.2.0", "version: 0.1.0"
        )
        if "version: 0.1.0" not in mismatch:
            pytest.skip("capability.yaml layout does not contain 'version: 0.2.0' for replace")
        (temp_project_full / "skills" / "skillweave-blueprint" / "capability.yaml").write_text(mismatch)
        syncer = CapaciumManifestSync(repo_root=str(temp_project_full))
        issues = syncer.check()
        assert issues
        assert any(
            "skillweave-blueprint" in issue.path or "capability.yaml" in issue.path
            for issue in issues
        )

    def test_write_repairs_drift(self, temp_project_full):
        (temp_project_full / "capability.yaml").write_text(
            (temp_project_full / "capability.yaml").read_text().replace("version: 0.2.0", "version: 0.1.0", 1)
        )
        syncer = CapaciumManifestSync(repo_root=str(temp_project_full))
        issues = syncer.write()
        assert issues
        assert syncer.check() == []
        root_manifest = (temp_project_full / "capability.yaml").read_text()
        assert "version: 0.2.0" in root_manifest
        assert "source: ./skills/skillweave-blueprint" in root_manifest


# ─── Changelog Tests ───────────────────────────────────────────────────


class TestChangelogGenerator:
    def test_parse_conventional_commit(self):
        gen = ChangelogGenerator()
        entry = gen.parse_commit("feat: add new feature", "abc1234")
        assert entry is not None
        assert entry.type == "feat"
        assert entry.category == "Features"

    def test_parse_with_scope(self):
        gen = ChangelogGenerator()
        entry = gen.parse_commit("fix(api): handle edge case")
        assert entry is not None
        assert entry.scope == "api"
        assert entry.description == "handle edge case"

    def test_parse_breaking(self):
        gen = ChangelogGenerator()
        entry = gen.parse_commit("feat!: breaking change")
        assert entry is not None
        assert entry.breaking is True

    def test_parse_non_conventional(self):
        gen = ChangelogGenerator()
        entry = gen.parse_commit("some random message")
        assert entry is None

    def test_parse_multiple_commits(self):
        gen = ChangelogGenerator()
        messages = [
            ("a1", "feat: new feature"),
            ("b2", "fix: bug fix"),
            ("c3", "docs: update docs"),
            ("d4", "random note"),
        ]
        entries = gen.parse_commits(messages)
        assert len(entries) == 3

    def test_generate_groups_by_category(self):
        gen = ChangelogGenerator()
        entries = [
            CommitEntry(sha="a1", type="feat", scope=None, description="Feature A"),
            CommitEntry(sha="b2", type="fix", scope=None, description="Fix B"),
            CommitEntry(sha="c3", type="feat", scope=None, description="Feature C"),
        ]
        result = gen.generate(entries, version="1.0.0")
        assert result.version == "1.0.0"
        assert result.total_commits == 3
        assert len(result.sections) >= 2

    def test_generates_markdown(self):
        gen = ChangelogGenerator()
        entries = [
            CommitEntry(sha="a1", type="feat", scope=None, description="Feature A"),
            CommitEntry(sha="b2", type="fix", scope=None, description="Fix B"),
        ]
        result = gen.generate(entries, version="1.0.0")
        md = gen.generate_markdown(result)
        assert "[1.0.0]" in md
        assert "Features" in md

    def test_generates_json(self):
        gen = ChangelogGenerator()
        entries = [CommitEntry(sha="a1", type="feat", scope=None, description="Feature A")]
        result = gen.generate(entries, version="1.0.0")
        data = json.loads(gen.generate_json(result))
        assert data["version"] == "1.0.0"
        assert data["total_commits"] == 1

    def test_detects_breaking_changes(self):
        gen = ChangelogGenerator()
        entries = [CommitEntry(sha="a1", type="feat", scope=None, description="Break", breaking=True)]
        result = gen.generate(entries)
        assert result.has_breaking_changes is True


# ─── Issue Manager Tests ───────────────────────────────────────────────


class TestIssueManager:
    def test_detect_closed_issues(self):
        mgr = IssueManager()
        messages = [
            ("sha1", "fix: resolve bug"),
            ("sha2", "closes #42"),
            ("sha3", "Fixes #100 and #101"),
            ("sha4", "Resolved #200"),
        ]
        closed = mgr.detect_closed_issues(messages)
        assert len(closed) == 3

    def test_analyze_commits_finds_features(self):
        mgr = IssueManager()
        result = mgr.analyze_commits([
            ("a1", "feat: amazing feature"),
            ("b2", "fix: bug fix"),
            ("c3", "fixes #10"),
        ])
        assert len(result.suggestions) >= 1
        assert len(result.closed_issues) >= 1

    def test_generates_json(self):
        mgr = IssueManager()
        result = mgr.analyze_commits([
            ("a1", "feat: feature"),
            ("b2", "fixes #5"),
        ])
        data = json.loads(mgr.generate_json(result))
        assert len(data["suggestions"]) >= 1
        assert len(data["closed_issues"]) >= 1

    def test_scans_todos(self):
        mgr = IssueManager()
        content = """
# TODO: implement caching layer
# FIXME: this is broken
# TODO: add tests
"""
        suggestions = mgr.scan_todos(content, "src/main.py")
        assert len(suggestions) >= 2

    def test_generates_issue_body(self):
        mgr = IssueManager()
        suggestion = IssueSuggestion(title="Test", body="Body text", labels=["bug"])
        body = mgr.generate_issue_body(suggestion)
        assert "Body text" in body
        assert "bug" in body


# ─── PR Description Tests ──────────────────────────────────────────────


class TestPRDescriptionGenerator:
    def test_generates_from_commits(self):
        gen = PRDescriptionGenerator()
        result = gen.generate_from_commits([
            ("a1", "feat: add feature"),
            ("b2", "fix: fix bug"),
            ("c3", "docs: update readme"),
        ], branch_name="feature/test-branch")
        assert result.commit_count == 3
        assert len(result.sections) >= 2

    def test_generates_title_from_branch(self):
        gen = PRDescriptionGenerator()
        result = gen.generate_from_commits([], branch_name="fix/login-bug")
        assert "Fix: Login Bug" in result.title or "Login Bug" in result.title

    def test_generates_title_from_commits(self):
        gen = PRDescriptionGenerator()
        result = gen.generate_from_commits([
            ("a1", "feat: add login feature"),
        ])
        assert "add login feature" in result.title

    def test_detects_breaking(self):
        gen = PRDescriptionGenerator()
        result = gen.generate_from_commits([
            ("a1", "feat!: break api"),
        ])
        assert result.has_breaking_changes is True

    def test_generates_markdown(self):
        gen = PRDescriptionGenerator()
        result = gen.generate_from_commits([
            ("a1", "feat: feature"),
        ])
        md = gen.generate_markdown(result)
        assert "Summary" in md
        assert "Checklist" in md

    def test_generates_json(self):
        gen = PRDescriptionGenerator()
        result = gen.generate_from_commits([
            ("a1", "feat: feature"),
        ])
        data = json.loads(gen.generate_json(result))
        assert data["commit_count"] == 1


# ─── Docs Sync Tests ───────────────────────────────────────────────────


class TestDocsSynchronizer:
    def test_scan_detects_functions(self):
        root = Path(tempfile.mkdtemp())
        src = root / "src" / "app"
        src.mkdir(parents=True)
        (src / "module.py").write_text(
            "def foo():\n    '''Docstring.'''\n    pass\n"
            "def bar():\n    pass\n"
        )
        syncer = DocsSynchronizer(repo_root=str(root))
        result = syncer.scan_python_files()
        assert result.total_functions == 2
        assert len(result.missing_docs) >= 1
        shutil.rmtree(str(root))

    def test_scan_detects_classes(self):
        root = Path(tempfile.mkdtemp())
        src = root / "src" / "app"
        src.mkdir(parents=True)
        (src / "models.py").write_text(
            'class User:\n    """User model."""\n    pass\n'
            'class Product:\n    pass\n'
        )
        syncer = DocsSynchronizer(repo_root=str(root))
        result = syncer.scan_python_files()
        assert result.total_functions == 2
        assert result.documented >= 1
        shutil.rmtree(str(root))

    def test_generates_markdown(self):
        root = Path(tempfile.mkdtemp())
        src = root / "src" / "app"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("def foo():\n    pass\n")
        syncer = DocsSynchronizer(repo_root=str(root))
        result = syncer.scan_python_files()
        md = syncer.generate_markdown(result)
        assert "Documentation Sync Report" in md
        shutil.rmtree(str(root))

    def test_generates_json(self):
        root = Path(tempfile.mkdtemp())
        src = root / "src" / "app"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("def foo():\n    '''Doc.'''\n    pass\n")
        syncer = DocsSynchronizer(repo_root=str(root))
        result = syncer.scan_python_files()
        data = json.loads(syncer.generate_json(result))
        assert data["total_functions"] >= 1
        shutil.rmtree(str(root))

    def test_no_source_dir(self):
        root = Path(tempfile.mkdtemp())
        syncer = DocsSynchronizer(repo_root=str(root))
        result = syncer.scan_python_files()
        assert len(result.errors) > 0
        shutil.rmtree(str(root))

    def test_coverage_calculation(self):
        root = Path(tempfile.mkdtemp())
        src = root / "src" / "app"
        src.mkdir(parents=True)
        (src / "mod.py").write_text(
            'def a():\n    """Doc."""\n    pass\n'
            'def b():\n    pass\n'
            'def c():\n    """Doc."""\n    pass\n'
        )
        syncer = DocsSynchronizer(repo_root=str(root))
        result = syncer.scan_python_files()
        assert result.coverage_pct == 100.0
        shutil.rmtree(str(root))


# ─── Release Gate Tests ────────────────────────────────────────────────


class TestReleaseReadinessGate:
    def test_check_version_bump_new(self):
        gate = ReleaseReadinessGate()
        check = gate.check_version_bump("0.6.0", "v0.5.0")
        assert check.passed is True
        assert "0.5.0" in check.detail

    def test_check_version_bump_same(self):
        gate = ReleaseReadinessGate()
        check = gate.check_version_bump("0.5.0", "v0.5.0")
        assert check.passed is False

    def test_check_version_bump_no_tag(self):
        gate = ReleaseReadinessGate()
        check = gate.check_version_bump("0.5.0", None)
        assert check.passed is True

    def test_check_changelog_exists(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_changelog("0.2.0")
        assert check.passed is True

    def test_check_changelog_accepts_v_prefixed_heading(self, temp_project_full):
        # The project's real CHANGELOG headings are ``## vX.Y.Z``; the check
        # must recognise that form, not just ``## X.Y.Z`` / ``## [X.Y.Z]``.
        changelog = temp_project_full / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## v0.2.0 (2026-04-27) — Release\n### Added\n- Foo\n")
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_changelog("0.2.0")
        assert check.passed is True, check.detail
        assert "0.2.0" in check.detail

    def test_check_changelog_reports_other_entries_exist(self, temp_project_full):
        # A version entry in the project's v-prefixed form must be seen as an
        # *other* entry (not "No version entries found") when the requested
        # version is genuinely absent.
        changelog = temp_project_full / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## v0.2.0 (2026-04-27) — Release\n### Added\n- Foo\n")
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_changelog("9.9.9")
        assert check.passed is False
        assert "other entries exist" in check.detail

    def test_check_changelog_missing_entry(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_changelog("9.9.9")
        assert check.passed is False

    def test_check_changelog_no_file(self):
        root = Path(tempfile.mkdtemp())
        gate = ReleaseReadinessGate(repo_root=str(root))
        check = gate.check_changelog("0.1.0")
        assert check.passed is False
        shutil.rmtree(str(root))

    def test_check_tests_exist(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_tests()
        assert check.passed is True
        assert "1 test files" in check.detail

    def test_check_tests_missing(self):
        root = Path(tempfile.mkdtemp())
        (root / "src").mkdir()
        gate = ReleaseReadinessGate(repo_root=str(root))
        check = gate.check_tests()
        assert check.passed is False
        shutil.rmtree(str(root))

    def test_check_required_files(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        checks = gate.check_required_files(["README.md", "LICENSE", "MISSING.txt"])
        assert checks[0].passed is True
        assert checks[1].passed is True
        assert checks[2].passed is False

    def test_check_wip_markers_clean(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_wip_markers(["src"])
        assert check.passed is True

    def test_check_wip_markers_found(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        (temp_project_full / "src" / "wip_file.py").write_text(
            "# TODO: finish this later\n# WIP: not done yet\n"
        )
        check = gate.check_wip_markers(["src"])
        assert check.passed is False
        assert check.detail.startswith("Found")

    def test_check_capacium_manifests(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_capacium_manifests()
        assert check.passed is True

    def test_check_capacium_manifests_detects_drift(self, temp_project_full):
        drifted = temp_project_full / "skills" / "skillweave-blueprint" / "capability.yaml"
        drifted.write_text(drifted.read_text().replace("version: 0.2.0", "version: 0.1.0"))
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_capacium_manifests()
        assert check.passed is False
        assert "out of sync" in check.detail

    def test_check_release_version_matches_all_capability_manifests(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_release_version_matches_capabilities("0.2.0")
        assert check.passed is True
        assert "2 capability manifest" in check.detail

    def test_check_release_version_passes_when_member_lags_release(self, temp_project_full):
        # decoupled_member_pins: a member file and its bundle pin may lag the
        # release version together (a packaging-only bump). That is not a
        # release-version mismatch, so the check stays green.
        pyproject = temp_project_full / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('version = "0.2.0"', 'version = "0.3.0"'))
        root = temp_project_full / "capability.yaml"
        root.write_text(root.read_text().replace("version: 0.2.0", "version: 0.3.0", 1))
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_release_version_matches_capabilities("0.3.0")
        assert check.passed is True, check.detail
        assert "0.3.0" in check.detail

    def test_check_release_version_blocks_skill_manifest_pin_mismatch(self, temp_project_full):
        # A member file contradicting its own bundle pin is a real failure:
        # the pin promises which member version the bundle ships.
        drifted = temp_project_full / "skills" / "skillweave-blueprint" / "capability.yaml"
        drifted.write_text(drifted.read_text().replace("version: 0.2.0", "version: 0.1.0"))
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_release_version_matches_capabilities("0.2.0")
        assert check.passed is False
        assert "skillweave-blueprint/capability.yaml" in check.detail

    def test_check_release_version_blocks_bundle_version_mismatch(self, temp_project_full):
        # The distribution bundle is a required release surface: it must carry
        # the release version.
        root = temp_project_full / "capability.yaml"
        root.write_text(root.read_text().replace("version: 0.2.0", "version: 0.1.0", 1))
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        check = gate.check_release_version_matches_capabilities("0.2.0")
        assert check.passed is False
        assert "capability.yaml=0.1.0" in check.detail

    def test_evaluate_passes(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        result = gate.evaluate(current_version="0.2.0", latest_tag="v0.1.0")
        assert result.all_required_passed is True

    def test_evaluate_fails_version_not_bumped(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        result = gate.evaluate(current_version="0.2.0", latest_tag="v0.2.0")
        assert result.all_required_passed is False

    def test_generates_markdown(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        result = gate.evaluate(current_version="0.2.0", latest_tag="v0.1.0")
        md = gate.generate_markdown(result)
        assert "Release Readiness Gate Report" in md

    def test_generates_json(self, temp_project_full):
        gate = ReleaseReadinessGate(repo_root=str(temp_project_full))
        result = gate.evaluate(current_version="0.2.0", latest_tag="v0.1.0")
        data = json.loads(gate.generate_json(result))
        assert "can_release" in data


# ─── Release Notes Tests ───────────────────────────────────────────────


class TestReleaseNotesGenerator:
    def test_generates_markdown(self):
        gen = ReleaseNotesGenerator()
        notes = gen.generate(version="0.6.0")
        assert isinstance(notes, str)
        assert len(notes) > 50

    def test_generates_json(self):
        gen = ReleaseNotesGenerator()
        data = json.loads(gen.generate_json(version="0.6.0"))
        assert "version" in data
        assert "title" in data


# ─── Import Sanity Tests ───────────────────────────────────────────────


class TestImports:
    def test_all_modules_importable(self):
        import importlib
        modules = [
            "skillweave.github_integration",
            "skillweave.github_integration.inventory",
            "skillweave.github_integration.autotag",
            "skillweave.github_integration.changelog",
            "skillweave.github_integration.issue_manager",
            "skillweave.github_integration.pr_description",
            "skillweave.github_integration.docs_sync",
            "skillweave.github_integration.release_gate",
            "skillweave.github_integration.release_notes",
        ]
        for mod_name in modules:
            importlib.import_module(mod_name)

    def test_all_classes_accessible_from_package(self):
        from skillweave.github_integration import (
            WorkflowInventory, AutoTagger, ChangelogGenerator,
            IssueManager, PRDescriptionGenerator, DocsSynchronizer,
            ReleaseReadinessGate, ReleaseNotesGenerator,
        )
        assert WorkflowInventory is not None
        assert AutoTagger is not None
        assert ChangelogGenerator is not None
        assert IssueManager is not None
        assert PRDescriptionGenerator is not None
        assert DocsSynchronizer is not None
        assert ReleaseReadinessGate is not None
        assert ReleaseNotesGenerator is not None
