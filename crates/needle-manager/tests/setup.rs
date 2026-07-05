//! Setup wizard scenarios against a throwaway NEEDLE_HOME.
//!
//! Hermetic: the worker source is a tiny dependency-free package, the model
//! dir is pre-seeded, and NEEDLE_DEV_PI_BIN points nowhere so the wizard can
//! never touch a real Pi config from tests.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("ns-{}-{label}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch");
    dir
}

/// Minimal installable package that provides `import needle_worker`.
fn fake_worker_source(dir: &Path) -> PathBuf {
    let source = dir.join("worker-src");
    let package = source.join("needle_worker");
    std::fs::create_dir_all(&package).expect("create fake worker package");
    std::fs::write(
        source.join("pyproject.toml"),
        r#"[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "needle-worker"
version = "0.0.1"

[tool.setuptools]
packages = ["needle_worker"]
"#,
    )
    .expect("write pyproject");
    std::fs::write(package.join("__init__.py"), "").expect("write __init__");
    source
}

fn fake_model_dir(dir: &Path) -> PathBuf {
    let model = dir.join("model");
    std::fs::create_dir_all(&model).expect("create model dir");
    std::fs::write(model.join("needle-model.json"), "{}").expect("write provenance");
    model
}

fn fake_pi_with_registered_packages(dir: &Path) -> PathBuf {
    let script = dir.join("fake-pi.sh");
    std::fs::write(
        &script,
        r#"#!/bin/sh
case "$1" in
  --version)
    echo "0.73.1"
    ;;
  list)
    echo "User packages:"
    echo "  $PI_OLD_SOURCE"
    echo "    $PI_OLD_RESOLVED"
    echo "  $PI_CURRENT_SOURCE"
    echo "    $PI_CURRENT_RESOLVED"
    ;;
  install|uninstall|remove)
    echo "$@" >> "$PI_CALLS"
    ;;
  *)
    echo "$@" >> "$PI_CALLS"
    ;;
esac
"#,
    )
    .expect("write fake pi");
    let mut perms = std::fs::metadata(&script)
        .expect("stat fake pi")
        .permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&script, perms).expect("chmod fake pi");
    script
}

fn needle_setup(home: &Path, dir: &Path, args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("setup")
        .args(args)
        .env("NEEDLE_HOME", home)
        .env("NEEDLE_DEV_WORKER_SOURCE", fake_worker_source(dir))
        .env("NEEDLE_MODEL_DIR", fake_model_dir(dir))
        .env("NEEDLE_DEV_PI_BIN", "/nonexistent/pi")
        .output()
        .expect("run needle setup")
}

fn needle_setup_without_model_override(
    home: &Path,
    dir: &Path,
    args: &[&str],
) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("setup")
        .args(args)
        .env("NEEDLE_HOME", home)
        .env("NEEDLE_DEV_WORKER_SOURCE", fake_worker_source(dir))
        .env_remove("NEEDLE_MODEL_DIR")
        .env("NEEDLE_DEV_PI_BIN", "/nonexistent/pi")
        .output()
        .expect("run needle setup")
}

#[test]
fn dry_run_touches_nothing() {
    let dir = scratch("dry");
    let home = dir.join("home");

    let output = needle_setup(&home, &dir, &["--dry-run", "--yes"]);

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success(), "stdout: {stdout}");
    assert!(stdout.contains("dry run"), "stdout: {stdout}");
    assert!(!home.exists(), "dry run created NEEDLE_HOME");
}

#[test]
fn dry_run_fresh_home_reports_planned_model_download() {
    let dir = scratch("dry-fresh");
    let home = dir.join("home");

    let output = needle_setup_without_model_override(&home, &dir, &["--dry-run", "--yes"]);

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "stdout: {stdout}\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains("would run `python -m needle_worker.model_download_cli`"),
        "model dry-run did not reach planned download: {stdout}"
    );
    assert!(!home.exists(), "dry run created NEEDLE_HOME");
}

#[test]
fn full_setup_provisions_home_and_is_idempotent() {
    let dir = scratch("full");
    let home = dir.join("home");

    let output = needle_setup(&home, &dir, &["--yes"]);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "stdout: {stdout}\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    // The install-story layout, minus what this hermetic run skips.
    let venv_python = home.join("python").join("venv").join("bin").join("python");
    assert!(venv_python.exists(), "venv python missing");
    assert!(home.join("config.json").exists(), "config.json missing");
    assert!(home.join("logs").is_dir(), "logs dir missing");
    let import = Command::new(&venv_python)
        .args(["-c", "import needle_worker"])
        .status()
        .expect("run venv python");
    assert!(import.success(), "needle_worker not importable from venv");

    let config: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(home.join("config.json")).unwrap())
            .expect("config parses");
    assert_eq!(
        config["worker_python"].as_str().map(PathBuf::from),
        Some(venv_python.clone())
    );
    assert!(config["model_dir"].as_str().unwrap_or("").contains("model"));
    assert_eq!(config["pi_integrated"], false);

    // Second run: every step reports already-done, nothing re-provisions.
    let rerun = needle_setup(&home, &dir, &["--yes"]);
    let stdout = String::from_utf8_lossy(&rerun.stdout);
    assert!(rerun.status.success(), "stdout: {stdout}");
    assert!(
        stdout.contains("already provisioned"),
        "worker step not idempotent: {stdout}"
    );
    assert!(
        stdout.contains("using NEEDLE_MODEL_DIR"),
        "model step not idempotent: {stdout}"
    );
}

#[test]
fn setup_replaces_stale_needle_pi_registration() {
    let dir = scratch("stale-pi");
    let home = dir.join("home");
    let current = home.join("pi");
    let calls = dir.join("pi-calls.txt");
    let old_resolved = dir
        .join("old-homebrew")
        .join("Cellar/needle/HEAD-1611dc9/libexec/lib/python3.13/site-packages/needle/hosts/pi");
    std::fs::create_dir_all(&current).expect("create current pi target");
    std::fs::create_dir_all(&old_resolved).expect("create old pi package");
    std::fs::create_dir_all(&home).expect("create home");
    std::fs::write(
        home.join("config.json"),
        serde_json::json!({
            "pi_integrated": true
        })
        .to_string(),
    )
    .expect("write config");

    let old_source = "../../../../opt/homebrew/Cellar/needle/HEAD-1611dc9/libexec/lib/python3.13/site-packages/needle/hosts/pi";
    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("setup")
        .arg("--yes")
        .env("NEEDLE_HOME", &home)
        .env("NEEDLE_DEV_WORKER_SOURCE", fake_worker_source(&dir))
        .env("NEEDLE_MODEL_DIR", fake_model_dir(&dir))
        .env("NEEDLE_DEV_PI_BIN", fake_pi_with_registered_packages(&dir))
        .env("PI_CALLS", &calls)
        .env("PI_OLD_SOURCE", old_source)
        .env("PI_OLD_RESOLVED", &old_resolved)
        .env("PI_CURRENT_SOURCE", &current)
        .env("PI_CURRENT_RESOLVED", &current)
        .output()
        .expect("run needle setup");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "stdout: {stdout}\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains("will remove stale Needle Pi package"),
        "stale package warning missing: {stdout}"
    );
    let pi_calls = std::fs::read_to_string(&calls).expect("pi calls");
    assert!(
        pi_calls.contains(&format!("uninstall {old_source}")),
        "old package was not uninstalled: {pi_calls}"
    );
    assert!(
        pi_calls.contains(&format!("install {}", current.display())),
        "current package was not installed: {pi_calls}"
    );

    let config: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(home.join("config.json")).unwrap())
            .expect("config parses");
    assert_eq!(config["pi_integrated"], true);
}

#[test]
fn bare_needle_runs_wizard_when_unconfigured() {
    let dir = scratch("bare");
    let home = dir.join("home");

    // Bare `needle` on an unconfigured home enters the wizard. Prompts read
    // EOF from a closed stdin and decline, so nothing heavy happens; the
    // wizard banner is what we are asserting.
    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .env("NEEDLE_HOME", &home)
        .env("NEEDLE_DEV_WORKER_SOURCE", fake_worker_source(&dir))
        .env("NEEDLE_MODEL_DIR", fake_model_dir(&dir))
        .env("NEEDLE_DEV_PI_BIN", "/nonexistent/pi")
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run bare needle");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("needle setup"), "stdout: {stdout}");
    assert!(stdout.contains("[1/5] system check"), "stdout: {stdout}");
}
