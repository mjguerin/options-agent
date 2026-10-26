"""Experiment manifest and the startup preflight (FR-01, FR-09, FR-13, NFR-15, AC-21, AC-23)."""

import subprocess
from datetime import UTC, datetime

import pytest
import yaml

from tournament.accounts.manifest import (
    Approval,
    Binding,
    Manifest,
    ManifestError,
    approve,
    current_hashes,
    draft_manifest,
    load_manifest,
    preflight,
    write_manifest,
)

HASHES = {"app": "a" * 64, "risk": "b" * 64}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("one")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "one")
    git(r, "tag", "exp-1")
    return r


def manifest(mode="paper", tag="exp-1", approved=True, accounts=None):
    return Manifest(
        experiment_id="exp-1",
        mode=mode,
        git_tag=tag,
        starting_balance_usd=10_000.0,
        accounts=accounts
        or [Binding(strategy_id=s, alias=s) for s in ("vpc", "orb", "brt", "fbr", "vmr")],
        config_hashes=HASHES,
        approved=Approval(by="owner", at="2026-10-26T12:00:00+00:00")
        if approved
        else Approval(by=None, at=None),
    )


def run(m, repo, feed="opra", hashes=HASHES):
    return preflight(m, repo, options_feed=feed, hashes=hashes)


def failed(result):
    return {c.name for c in result.failures}


# ---- creating and keeping manifests ---------------------------------------------------------


def test_a_draft_covers_every_config_the_run_depends_on(tmp_path):
    m = draft_manifest("exp-9", "shadow", None, 10_000.0)
    names = set(m.config_hashes)
    assert {"app", "feed", "risk", "orders", "features", "common", "eligibility", "schema"} <= names
    assert {f"strategy_{s}" for s in ("vpc", "orb", "brt", "fbr", "vmr")} <= names
    assert m.config_hashes == current_hashes() and m.approved.by is None


def test_a_manifest_is_immutable_and_approval_changes_nothing_else(tmp_path):
    m = draft_manifest("exp-9", "shadow", None, 10_000.0)
    path = write_manifest(m, tmp_path / "exp-9.yaml")
    with pytest.raises(ManifestError, match="immutable"):
        write_manifest(m, path)
    before = yaml.safe_load(path.read_text())
    approve(path, "owner", datetime(2026, 10, 26, 12, tzinfo=UTC))
    after = yaml.safe_load(path.read_text())
    assert {k: v for k, v in after.items() if k != "approved"} == {
        k: v for k, v in before.items() if k != "approved"
    }
    loaded = load_manifest(path)
    assert loaded.approved.by == "owner" and loaded.manifest_hash == m.manifest_hash


def test_a_paper_manifest_must_name_its_code_tag():
    with pytest.raises(ValueError):
        manifest(mode="paper", tag=None)


# ---- the preflight ----------------------------------------------------------------------------


def test_a_clean_tagged_approved_paper_manifest_passes(repo):
    r = run(manifest(), repo)
    assert r.passed and r.failures == [] and r.commit_sha is not None


def test_a_dirty_tree_blocks_paper_but_only_warns_in_shadow(repo):  # AC-21
    (repo / "f.txt").write_text("edited")
    paper = run(manifest(), repo)
    assert not paper.passed and "git_tree_clean" in failed(paper)
    shadow = run(manifest(mode="shadow", approved=False), repo)
    assert shadow.passed and "git_tree_clean" in failed(shadow)  # recorded, not blocking


def test_head_must_be_at_the_manifest_tag_for_paper(repo):  # AC-21
    (repo / "f.txt").write_text("two")
    git(repo, "commit", "-q", "-am", "two")
    r = run(manifest(), repo)
    assert not r.passed and failed(r) == {"head_at_manifest_tag"}


def test_a_paper_manifest_needs_an_approval(repo):
    r = run(manifest(approved=False), repo)
    assert not r.passed and failed(r) == {"approved"}


def test_a_config_edited_after_approval_stops_the_run(repo):  # FR-09
    r = run(manifest(), repo, hashes={**HASHES, "risk": "c" * 64})
    assert not r.passed and "config_risk" in failed(r)
    extra = run(manifest(), repo, hashes={**HASHES, "orders": "d" * 64})
    assert "config_complete" in failed(extra)  # a config the manifest doesn't know about


@pytest.mark.parametrize("mode", ["paper", "shadow"])
def test_the_indicative_options_feed_is_refused(repo, mode):  # AC-23, MD-09
    r = run(manifest(mode=mode, approved=True), repo, feed="indicative")
    assert not r.passed and "options_feed" in failed(r)


def test_account_bindings_must_be_unique_and_complete(repo):  # FR-01
    dup = [Binding(strategy_id=s, alias="same") for s in ("vpc", "orb", "brt", "fbr", "vmr")]
    assert "unique_account_bindings" in failed(run(manifest(accounts=dup), repo))
    four = [Binding(strategy_id=s, alias=s) for s in ("vpc", "orb", "brt", "fbr")]
    assert "all_strategies_bound" in failed(run(manifest(accounts=four), repo))
